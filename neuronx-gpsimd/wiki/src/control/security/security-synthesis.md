# SEC-Lane Synthesis (boot → attest → fault)

This is the **capstone** of the security lane: a single coherent security model
for the GPSIMD engine (Cadence Tensilica **Vision-Q7 NX** "Cairo" DSP, core ID
`ncore2gp`, the eight POOL cores of one NeuronCore). It does not introduce new
mechanisms — it **threads the seven committed sibling pages into one narrative**
and re-grounds every central fact against the shipped binaries, then folds the
**BOOT → ATTEST → FAULT → RECOVER** chain into one page with an honest verdict on
what protects what.

Every primary claim is attributed to the sibling that decoded it and re-confirmed
here. Where a count or offset matters, it was re-verified this pass with `nm` /
`readelf` / a byte scan against the shipped artifact (the decompile is **never**
used for counts — it inflates 2–12×). Confidence tags: **HIGH** = byte-read from a
shipped artifact and re-verified; **MED** = a security characterisation that
follows from observed facts; **LOW** = plausible, flagged. Evidence:
**OBSERVED** = read directly this pass; **INFERRED** = reasoned over observed
bytes; **CARRIED** = consolidated from a named sibling without re-deriving the
disasm here. v5 / Maverick interiors are flagged **INFERRED**.

The committed siblings this page reconciles:

* [`boot-fault-overview.md`](boot-fault-overview.md) — the on-core XEA3 fault map + the partial SEQ override.
* [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md) — the byte-exact remapper / NSM / qos-NTS perimeter.
* [`boot-arming-fault-recovery.md`](boot-arming-fault-recovery.md) — the reset-default-LAX → armed transition + recovery SM.
* [`profiling-trace-debug-gating.md`](profiling-trace-debug-gating.md) — why the rich measurement surface is host-only.
* [`reachability-isolation.md`](reachability-isolation.md) — the capability-table / translate / PSUM reachability cuts.
* [`trust-chain-threat-model.md`](trust-chain-threat-model.md) — the 7-stage load chain + actor model + silent override.
* [`side-channel-leakage.md`](side-channel-leakage.md) — the four co-tenancy leakage channels + the tiered findings.

---

## 0. The model in one sentence

> GPSIMD is a **fabric-isolated, on-core-unprivileged** compute engine whose
> firmware is **admitted by integrity (not signed)** and **installed by a silent,
> unauthenticated override** — so the device **trusts the host by design**. Its
> real, hardware-enforced security boundary is the **SoC PCIe fabric perimeter**
> and the **NeuronCore**, *not* the Vision-Q7 compute core and *not* a
> cryptographic root of trust. It **boots armed-not-default-secure**, **attests
> nothing**, and **faults fail-stop**.

Three independent structural truths make that sentence true:

* **Trust is integrity-rooted, not key-rooted.** Every gate on the device load
  path is structural, version, or *unkeyed* hash. There is no signature to fail,
  no measured boot, no key. [HIGH — [`trust-chain-threat-model.md`](trust-chain-threat-model.md)]
* **The strong boundary is one level out.** Hardware access control lives in the
  SoC fabric (the remapper twins + NSM + qos/NTS); the on-core privilege model is
  deliberately minimal (single ring, identity-mapped, a 16+2 MPU). [HIGH — [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md) / [`reachability-isolation.md`](reachability-isolation.md)]
* **Faults are fail-stop, not silent-corrupt.** Every on-core violation except
  recoverable FP arithmetic is a permanent halt-and-report; off-core violations
  poison the AXI transaction and isolate the interface. [HIGH — [`boot-fault-overview.md`](boot-fault-overview.md)]

> **NOTE (scope).** Maverick (NC-v5) interiors are **INFERRED** from the Sunda /
> Cayman / Mariana pattern unless a Maverick artifact is cited directly; the
> `pass_on_miss` + `master_prot` invariants are confirmed frozen across
> Sunda → Cayman → Mariana but the Maverick remapper geometry is carried, not
> re-decoded here.

---

## 1. The three enforcement layers

Security is enforced at three concentric, mostly-independent layers, each with a
different threat model and a different substrate. The single most important
structural fact: the **strong** boundary is Layer A (the SoC fabric), **one level
out** from the compute core; the on-core Layer B is deliberately weak.

| Layer | Substrate | Enforces | What it stops | What it does **not** do |
|---|---|---|---|---|
| **A — SoC-fabric perimeter** | RTL CSRs (remapper / NSM / qos-NTS / PCIe iso SM) | hardware access control + protocol integrity | unauthorized AXI egress, malformed/hung traffic, a wedging interface | nothing about image *authenticity*; its guest half is reset-default lax |
| **B — On-core XEA3-MPU** | Vision-Q7 core (`ncore2gp`) | on-core out-of-rights access | a write to no-execute DRAM, a device-trap hole, a stack overflow → **fail-stop** | separate a custom kernel from base firmware or a co-resident op |
| **C — Firmware-load integrity gate** | host runtime + device loaders | structure + version + **unkeyed** integrity | corruption, a forward-incompatible artifact, a malformed ELF / wrong core count | detect a self-consistent forgery — admission ≠ authentication |

### 1A — The SoC-fabric perimeter (the only genuine HW access control)

Four cooperating blocks, each a different and non-overlapping job. The byte-exact
decode is in [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md); the summary
keystone:

| Mechanism | Role | What it stops |
|---|---|---|
| **`amzn_remapper`** (fail-**CLOSED**) | DECIDE | unauthorized egress on the **privileged** plane: a CAM on {masked address + 10-bit AXI-ID} → `rd_pass`/`wr_pass`; `pass_on_miss=0` ⇒ a miss **denies** (whitelist-by-default). Plus `bound_chk` (4 KiB span), `txn_len_chk` (`AWLEN`/`ARLEN` cap), rd/wr timeout |
| **`user_remapper`** (fail-**OPEN**) | DECIDE | unauthorized egress on the **guest** plane — the identical CAM IP, `pass_on_miss=1` at reset (a miss **passes**), permissive **until** the management core tightens it |
| **`qos_prot` NTS** | RESPOND | a stray/denied txn cannot wedge the fabric: terminates AXI with `SLVERR`/`DECERR` + `0xDEADBEEF` read-data. The remapper deny has no own error field — it **borrows** this path. **Not** an access gate |
| **NSM** (AXI watchdog) | POLICE | malformed / hung AXI: **9** sticky protocol-shape causes (spurious B/R, `rlast` errors, AW/W/AR/R timeouts) → inject `SLVERR`/`DECERR` + `0xDEADBEEF` + optionally drive the PCIe isolation SM. **Does not** gate on `AxPROT`/master-ID/VMID/address |

> **GOTCHA — all access-control gating is in the REMAPPER.** A full-file `jq` of
> `nsm.json` / `qos_prot.json` for `AxPROT` / master-ID / VMID / address returns
> **zero** — NSM and qos are an anti-hang / anti-corruption guard, **not** an
> access gate. Only the remapper decides who may emit which transaction.
> [HIGH/OBSERVED — [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md)]

The remapper twins are the **same IP instanced twice** per FIS as asymmetric
twins: `amzn` (privileged, fail-closed) + `user` (guest, fail-open); the
privileged twin **also holds management hooks over the guest twin** (CAM wipe,
trusted-ID bypass, the fail-open → fail-closed flip, full observability). This is
the firmware↔guest **trust boundary in hardware**, a frozen invariant across
Sunda → Cayman → Mariana (Maverick **INFERRED**).

> **CORRECTION — the `0xDEADBEEF` materialization is 8 / 1 / 0, qos = exactly ONE
> register.** Per the byte-exact `jq` count in
> [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md): **NSM materializes the
> poison in 8 registers, the NTS in exactly 1** (`nts_amzn.read_data` @ abs
> `0x40c`, one `val[31:0]` RW word, replicated in hardware across the bus width),
> and the **remapper in 0** (it borrows the NTS path). A raw
> `rg -ci deadbeef qos_prot.json` returns 2, but the second hit is the field
> *description* text, not a second register — **the qos `0xDEADBEEF` count is ×1,
> not ×2.** Any input asserting "×2 qos" is grep-inflation of the description and
> is corrected here to **×1**. [HIGH/OBSERVED]

The byte-exact errtrig surface for **Cayman = 962 total (428 USER + 534 AMZN)**
trigger entries is established in the perimeter sibling and carried here without
re-derivation. [HIGH/CARRIED — [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md)]

### 1B — The on-core XEA3-MPU (deliberately minimal)

Re-verified this pass against `ncore2gp` `core-isa.h`:

| Config knob | Value | Line | Consequence |
|---|---:|---:|---|
| `XCHAL_HAVE_SECURE` | `0` | 283 | **Cadence Secure Mode not instantiated** — no secure/non-secure world split |
| `XCHAL_MMU_RINGS` | `1` | 794 | a **single effective ring** — a custom kernel runs at the same ring as base firmware |
| `XCHAL_HAVE_IDENTITY_MAP` | `1` | 783 | `vaddr == paddr`, no ASIDs |
| `XCHAL_HAVE_PTP_MMU` | `0` | 787 | no page-table MMU |
| `XCHAL_HAVE_MPU` / `XCHAL_MPU_ENTRIES` | `1` / `16` | 800–801 | the MPU is the **only** on-core fence (16 foreground + 2 background, 4 KiB granule) |
| `XCHAL_HAVE_KSL` / `XCHAL_HAVE_ISL` | `1` / `1` | 710–711 | the one extra HW fence: a store below the installed stack limit is an unrecoverable `STACK_LIMIT` |
| `XCHAL_HAVE_XEA2` / `XCHAL_HAVE_XEA3` | `0` / `1` | 725–726 | **XEA3** — the exception model of §3 |

The MPU is programmed **once** at boot (`MPU_LOCK=0`, not re-keyed per op). The
boot map makes DRAM no-execute and carves device-trap holes; the background
default is permissive (`RWXrwx`) and the 16 foreground entries carve the deny
holes. **What it stops:** an on-core out-of-rights access → fail-stop. **What it
does not do:** it does not separate a custom kernel from base firmware or a
co-resident op — single ring, MPU not re-keyed per op, shared identity-mapped
space. On-core isolation **between** ops is software (serialization + a
well-formed image), not a HW ring. [HIGH/OBSERVED — re-verified this pass;
[`reachability-isolation.md`](reachability-isolation.md)]

> **QUIRK — `CCOUNT` is op-readable and cannot be revoked.** `XCHAL_HAVE_CCOUNT=1`
> (core-isa.h L455): a free-running cycle counter (SR 234) readable by `rsr ccount`
> from **any** code in the single-ring model. There is no privileged gate to deny
> an op the cycle counter — this is what turns shared-arbiter contention into a
> measurable timing channel (§5 MED-1). [HIGH/OBSERVED]

### 1C — The firmware-load integrity gate (no signing)

The custom-op admission path is **integrity + structural + version only** — no
cryptographic signature anywhere on the device load path. The seven stages are
decoded in [`trust-chain-threat-model.md`](trust-chain-threat-model.md); the three
load-path keystones were re-verified this pass in `libnrtucode_internal.so`:

```c
/* All three are local (t) symbols in libnrtucode_internal.so — nm-confirmed
 * at the exact offsets below. NONE of the three touches a key. */
bool xtlib_verify_magic(const void *img);       /* 0x9b6d40 — ELF magic + e_machine + phdr shape   */
int  validate_dynamic_load(load_ctx *c);        /* 0x9b71f0 — PT_DYNAMIC well-formedness            */
int  prelink_relocate_lib(load_ctx *c);         /* 0x9b6160 — applies R_XTENSA_RELATIVE (type 5),   */
                                                /*            self-relative, ABORTS on any failure  */
```

Integrity is therefore **ELF magic + phdr-shape + the self-relative
`R_XTENSA_RELATIVE` (type 5) relocation that aborts-on-fail**. The MD5/SHA-256 over
the gzip image is **unkeyed and optional** (skipped unless a per-load verify flag
is set). [HIGH/OBSERVED — `nm libnrtucode_internal.so` this pass]

> **NOTE — `nm -D libnrtucode_internal.so | rg -ci 'RSA_verify|ECDSA|X509|EVP_|ed25519|HMAC' = 0`,
> and `readelf -d` NEEDED = `libc.so.6` only.** The device-image load path links
> nothing cryptographic. The bundled OpenSSL under `c10/lib` is a PyTorch *host*
> build dependency (self-referential: only `libssl` + the afalg/padlock engine
> plug-ins consume `libcrypto`), linked-but-unreached. [HIGH/OBSERVED]

**What Layer C stops:** corruption (the hash), a forward-incompatible artifact
(the `feature_bits` trapdoor), a malformed ELF / wrong core-count. **What it does
not do:** it cannot detect a maliciously-crafted-but-self-consistent,
version-matching, well-formed image — **admission is not authentication.**

---

## 2. The trust model — no cryptographic root of trust on the load path

The defining property of the whole model, stated precisely.

### 2a. Integrity-rooted, not key-rooted

Every gate on the device load path is **structural** (size / ELF / relocation /
core-count), **version** (semver / feature-bitmap / arch), or **integrity**
(unkeyed MD5/SHA-256). There is no signature to fail, no measured boot, no image
attestation, no key. The unkeyed hash detects corruption; it cannot detect a
self-consistent forgery.

> **The honest reconcile — "no cryptographic root of trust" with "there IS
> hashing":** the load path **does** compute a hash (the optional MD5/SHA-256), but
> it is **unkeyed and non-attesting**, so it proves **integrity-of-transport**
> (the bytes arrived uncorrupted), **not authenticity** (the bytes are genuine).
> An unkeyed hash any party can recompute over forged bytes roots no trust.
> [HIGH facts / MED framing — [`trust-chain-threat-model.md`](trust-chain-threat-model.md)]

### 2b. The secmon capability exists in IP but is dead in silicon

The one Cadence trust substrate that *could* root a chain is present in the
toolchain but **deliberately not instantiated**: `XCHAL_HAVE_SECURE=0` (above),
and the secure-monitor (`secmon`) board libs ship **only** ldscripts +
`mpu_table.c` + memmap + specs — **zero compiled secure objects** — so the
`SECMON_SYSCALL` surface is never linked into a device image. The secmon object is
**available in the image but deliberately not linked into the live path.**
[HIGH/OBSERVED — `core-isa.h` L283 + secmon board-lib inventory;
[`trust-chain-threat-model.md`](trust-chain-threat-model.md)]

### 2c. The silent ucode override — the load chain's defining weakness

`nrt_set_pool_eng_ucode`, called before `nrt_init` with an arbitrary
`{iram, dram}` pair, causes the install seam to **unconditionally** overwrite the
stock POOL image — **no size / signature / arch check, no warning** — and the
device boots the substituted bytes and is claimed identically (same READY
sentinel). The **only** gate is the host-process capability to call `libnrt`; the
"before `nrt_init`" constraint is an **ordering rule, not an authorization**.

> **CORRECTION — the ucode install is a memhandle / MMIO write, NOT "BAR0 DMA".**
> The ucode stage into Q7 IRAM/DRAM is `write_padded` (`0x473eb0`) →
> `al_mem_write_buf` (`0x265990`) — a **memhandle / MMIO write**, not a
> BAR0-numbered DMA. The **only** BAR-numbered path on the load is **BAR4** = the
> zero-copy **weight** path. Any input describing the ucode install as "BAR0 DMA"
> is corrected: the install seam is an MMIO write; BAR4 is the weight path.
> [HIGH/OBSERVED — offsets carried from
> [`trust-chain-threat-model.md`](trust-chain-threat-model.md); BAR4 weight path
> per [`reachability-isolation.md`](reachability-isolation.md)]

### 2d. The fail-open vs fail-closed boundary (the keystone of the trust transition)

| Remapper | Plane | `pass_on_miss` @ reset | Posture |
|---|---|---|---|
| `user_remapper` | GUEST (`APB_IO`) | `rd`/`wr` = `0x1`/`0x1` | **fail-OPEN** — a CAM miss **passes**; permissive until tightened |
| `amzn_remapper` | SECURE (`PEB_APB_IO`) | `rd`/`wr` = `0x0`/`0x0` | **fail-CLOSED** — a CAM miss **denies**; the one block that boots secure |

This asymmetry **is** the firmware↔guest trust boundary in hardware: the firmware's
own egress is whitelist-by-default-secure; the guest's egress is allow-by-default
**until** the privileged management core ("Pacific" Q7) arms it. `AxPROT` is
amzn-only. [HIGH/OBSERVED — [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md) /
[`boot-arming-fault-recovery.md`](boot-arming-fault-recovery.md)]

### 2e. The perimeter is **armed-not-default-secure**

Out of reset the perimeter is **lax**: NSM in bypass, qos off (`chicken=1`), the
guest `user_remapper` fail-open. **Exactly one** of the perimeter blocks
(`amzn_remapper`) boots secure. The privileged management core arms the rest over
the `PEB_APB_IO` plane **before** the device is exposed to an untrusted host —
and the arming is **structurally out of the host's reach** (the sprot CSR bus is
deny-all except a single `MISC_RAM` aperture). The host is trusted to **stage**
images but **not** to **arm** the fabric. [HIGH/OBSERVED —
[`boot-arming-fault-recovery.md`](boot-arming-fault-recovery.md)]

### The actor model (who the device defends against)

| Actor | Controls | Bounded by | In scope? |
|---|---|---|---|
| **ACTOR-1** malicious NEFF | a compiled artifact a trusted host loads | integrity / structure / version gates + the forward-compat trapdoor | **stopped** (admission) |
| **ACTOR-2** malicious custom op | the device BIN inside an admitted ucode lib | on-core MPU/translate/assert (fail-stop) **and** off-core remapper CAM + unaddressable secure plane — but **not** isolated from a co-resident op | **bounded** (not sandboxed within a NeuronCore) |
| **ACTOR-3** malicious host process | can call `libnrt` directly | nothing — **it is the trust root** | **out of model** by design |

---

## 3. The fault / exception model (the RECOVER spine)

The chain's failure mode is **fail-stop**: a fault is detected, posted through the
XEA3 single-dispatch machine, fielded by the firmware `ErrorHandler` with a binary
recoverable/fatal policy, and reported to the host as a structured record.

### 3a. The XEA3 exception taxonomy — re-grounded

The exception model is **XEA3, not XEA2** (`core-isa.h` L726/L725). The state is
`EPC`/`EXCCAUSE`/`EXCVADDR`/`ISB`/`ISL`/`KSL`/`MS`/`VECBASE`, the exception vector
is `IEVEC 0x74`; the XEA2-only SRs (`EPC2-7`/`EPS2-7`/`INTENABLE`/`INTERRUPT`/`rsil`/`rfi`)
are **absent**. See [`../interrupt/xea3-interrupt-architecture.md`](../interrupt/xea3-interrupt-architecture.md).

`EXCCAUSE` is a 16-bit register: `[3:0]` CAUSE · `[7:4]` TYPE · `[11:8]` SUBTYPE ·
`[13:12]` LSFO · `[15:14]` IMPR, with `FULLTYPE[11:0] = (SUBTYPE<<8)|(TYPE<<4)|CAUSE`.
`XCHAL_EXCCAUSE_NUM = 9` (L730) — the 9 top-level CAUSE TYPEs are the table index
into the 9-entry `xtos_exc_handler_table`. **How each surfaces to the host:**

| CAUSE | `corebits-xea3.h` | Meaning | Installed handler | Host outcome |
|---:|---|---|---|---|
| `0` | `EXCCAUSE_NONE` | (spurious) | XTOS default `0x1c2ac` | — |
| `1` | `EXCCAUSE_INSTRUCTION` | illegal / unsupported / **privileged** inst | **custom SEQ `0x1a64`** | **FATAL** (code `'B'`) |
| `2` | `EXCCAUSE_ADDRESS` | load/store, alignment, gather/scatter | XTOS default `0x1c2ac` | FATAL / halt door |
| `3` | `EXCCAUSE_EXTERNAL` | bus/fetch error, **privileged ERI access** | **custom SEQ `0x1a64`** | **FATAL** (code `'B'`) |
| `4` | `EXCCAUSE_DEBUG` | break / single-step / **stack-limit** | **custom SEQ `0x1a64`** | **FATAL** (code `'B'`) |
| `5` | `EXCCAUSE_SYSCALL` | syscall | XTOS default `0x1c2ac` | FATAL / halt door |
| `6` | `EXCCAUSE_HARDWARE` | uncorrectable ECC / bus failure | XTOS default `0x1c2ac` | FATAL / halt door |
| `7` | `EXCCAUSE_MEMORY` | **MPU access violation / stack-limit** | XTOS default `0x1c2ac` | FATAL / halt door |
| `8` | `EXCCAUSE_CP_DISABLED` | coprocessor disabled | XTOS default `0x1c2ac` | FATAL / halt door |

> **GOTCHA — the SEQ HW-exception override is PARTIAL.** The firmware override
> only installs a custom handler for CAUSE **1 / 3 / 4** (→ SEQ `0x1a64`, FATAL
> code `'B'`). CAUSE **0 / 2 / 5 / 6 / 7 / 8 fall through to the XTOS default
> handler `0x1c2ac`** (a halt / simcall door). Both paths terminate FATAL, but the
> handler *body* differs by cause — a reimplementer must not assume one uniform
> custom handler. [HIGH/OBSERVED — [`boot-fault-overview.md`](boot-fault-overview.md)]

Representative security-relevant `EXC_TYPE_*` FULLTYPE codes (the silicon
superset; PT_*/L1_*/TLB-conflict codes are `[N/A]` on `ncore2gp` — no MMU, no
D-cache): `0x111 PRIV_INSTRUCTION` (the ring boundary), `0x211 PRIV_ERI_ACCESS`
(the ERI boundary), `0x121 DIVIDE_BY_ZERO`, `0x141..0x641` the six `FP_*`
sub-causes (**the one recoverable class**), `0x422 CROSS_MEM_ACCESS`,
`0x107/0x207/0x307 ACCESS_VIOLATION_{1,2,3}` (MPU prohibited),
`0x117/0x217/0x317 UNDEFINED_ATTR_{1,2,3}`, `0x064 STACK_LIMIT` (ISL/KSL),
`0x346/0x546` uncorrectable ECC. [HIGH/CARRIED — [`boot-fault-overview.md`](boot-fault-overview.md)]

### 3b. The ISS realization — the 61 `*_exc` handlers, re-counted

The host-side cycle-accurate model `libcas-core.so` realizes the complete fault
vocabulary as exactly **61** exception handlers — re-grounded this pass:

```text
$ nm libcas-core.so | rg -c '_exc$'
61
```

The 61 are byte-identical in the golden reference and absent from the
functional value oracle (which has no fault machine — value only). They map
cleanly onto the device 9-cause taxonomy (re-confirmed by enumerating the
symbols):

| Device CAUSE | `*_exc` family (sample of the 61) | n |
|---:|---|---:|
| 1 INSTRUCTION | `IllegalInstruction`, `Privileged`, `ExternalRegisterPrivilege`, `IntegerDivideByZero` (+ FP via the `Imprecise` path) | ~4 |
| 2 ADDRESS | the `LoadStore*` / `Inst*Fetch*` / `Sem*` families (`Alignment`, `AXIDec/AXISlv/BusError`, `Prohibited`, `UndefinedAttr`, `CrossMem`, `SGAcc`, `SlaveAtomic`, `TLBMultiHit`) | ~24 |
| 3 EXTERNAL | `InstAxiDec/Slv`, `LoadStoreAXIDec/Slv/Bus`, `ExclusiveError`, `SlaveAtomic` | ~8 |
| 4 DEBUG | `SingleStep`, `Break`/`BreakN`, `IBreak`/`DBreak`, `MaybeOCDBreak`, `OCDInterrupt`, `KSL`/`ISL`/`StackLimit` | ~10 |
| 5 SYSCALL | `SyscallException` | 1 |
| 7 MEMORY | MPU `Prohibited`/`UndefinedAttr`/`TLBMultiHit` (mirrored in the LoadStore family) | — |
| 8 CP_DISABLED | `Coprocessor0..6Exception` | 7 |
| pseudo | `Interrupt1`, `WindowOverflow8`/`WindowUnderflow8`, `Tailchain`/`L32ETailchain`, `WaitiFallThru`, `Halt`/`HaltStay`, `Imprecise`/`ImpreciseSGFPVec` | ~7 |

> **NOTE — the host model is access-mechanism-keyed; the device is cause-keyed.**
> The 61 ISS handlers are indexed by *access mechanism* (load vs store vs fetch),
> while the device `EXCCAUSE` is indexed by the 9 CAUSE nibbles; the host handler
> computes and posts the device FULLTYPE so the two reconcile. Validating
> **fault** behaviour (`EXCCAUSE`/`EXCVADDR`/`EPC`) requires the cas model or the
> device — the value oracle cannot. [HIGH/CARRIED — [`../interrupt/xea3-interrupt-architecture.md`](../interrupt/xea3-interrupt-architecture.md)]

### 3c. Delivery is POLLED, not vectored

`XCHAL_NUM_INTLEVELS = 7` (L460) — the core **supports** 7-level interrupt
delivery — but the firmware **deliberately disables** it: `rsil`/`rfi`/`INTENABLE`
are 0 in both images, so delivery is **polled, not vectored**. Interrupts and
exceptions merge into **one** XEA3 `DispatchVector`; per-source control is via
`RER`/`WER` over the ER block at `0x122000`, not the legacy `INTENABLE`/`INTSET`
SRs. `XCHAL_NUM_INTERRUPTS = 37`, `XCHAL_NUM_EXTINTERRUPTS = 25` (L457/L459). The
NSM → apex-IRQ flow is in [`../interrupt/nsm-flow-unified.md`](../interrupt/nsm-flow-unified.md).
[HIGH/OBSERVED — re-verified this pass]

### 3d. The firmware override — the SEQ ErrorHandler

| Entry | IRAM | Policy |
|---|---:|---|
| `HandleBadOpcode` | `0x13f58` | FATAL spin |
| `HandleIllegalInstr` | `0x13f80` | FATAL spin |
| `HandleIntDivZero` | `0x13f34` | FATAL spin |
| `HandleFPError` | `0x13eb0` | **RECOVERABLE return** (the only recoverable class) |
| `signal_handler` | `0x14014` | FATAL spin (catch-all for the 6 POSIX signals) |
| `assert_fail` | `0xa304` | FATAL spin |

`raise_error` (`0x13e18`) writes the packed record to **TIE `UR#0x15` (21)** (the
host-pollable error latch), then `sev-2 → Setup-Halt → infinite spin`; `sev-1` (FP
only) `→ retw.n` (continue). **Exactly one class — FP arithmetic — is
recoverable; everything else is a permanent halt-and-report.** Device-side decode:
[`../../firmware/seq/error-handler.md`](../../firmware/seq/error-handler.md);
the DGE OOB-bounds notification path:
[`../../firmware/dge/dge-errors.md`](../../firmware/dge/dge-errors.md).
[HIGH/CARRIED — [`boot-fault-overview.md`](boot-fault-overview.md)]

### 3e. How faults surface to the host (the device → host path)

Every fatal fault publishes a **16-byte `NEURON_ISA` notification** (`NBYTES=0x10`)
into the per-TPB notification ring + a **2-stage `comp_efd` wake** — there is **no
MSI/MSIX/IRQ/ISR** in `libnrt` (zero such symbols); the host drains the ring.

> **QUIRK — the host wake is `comp_efd` 2-stage, not an interrupt.** `libnrt` has
> **zero** `msi`/`msix`/`irq`/`isr` symbols; the device → host notification is a
> ring write + the 2-stage `comp_efd` eventfd wake the host polls. Do not model the
> host fault path as a hardware interrupt. [HIGH/OBSERVED]

The record discriminator `header.notific_type` uses `ERROR (0x03)` and
`TPB_ERROR (0x1f)`; the host-facing `error_id` (`NEURON_ISA_TPB_ERROR_TYPE`) runs
`0x00 FP_UNDERFLOW .. 0x0a SEQUENCER_FATAL`, with a Q7/SEQ fault mapping to
`SEQUENCER_NONFATAL (0x09)` / `SEQUENCER_FATAL (0x0a)`. The metadata subtype ties
the §3d codes to the host record:

| Device fault | Host subtype |
|---|---|
| `HandleBadOpcode` | `BAD_OPCODE (0x00)` |
| `signal_handler('A')` | `SIGNAL (0x41)` |
| HW exception | `EXCEPTION (0x42)` carrying `{Type:4, Cause:4}` = the §3a EXCCAUSE nibbles |
| `assert_fail` | `ASSERT (0x40)` |
| ucode version mismatch | `BAD_VERSION (0xb0/0x08)` |

**Off-core**, a remapper deny / NSM violation / NTS timeout / PCIe event surfaces
**differently** — to the management core, not the host runtime: it feeds the shared
host-PCIe isolation SM (`reset_handshake_intr[8..13] → enter[14]/exit[15]`),
poisons the master (`SLVERR`/`DECERR` + `0xDEADBEEF`), isolates the interface after
a debounce, drains the staging FIFOs, re-arms, and raises a **critical apex IRQ**
(`intr_peb_nsm_axi_timeout`, cause 111) to the "Pacific" management Q7. The
most-severe class drives a scan-dump + clock-stop + SRAM-write-protect post-mortem
freeze. [HIGH/CARRIED — [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md) /
[`boot-arming-fault-recovery.md`](boot-arming-fault-recovery.md)]

---

## 4. The custom-op capability bounds

A GPSIMD custom op runs SPMD on the 8 Vision-Q7 POOL cores of **one** NeuronCore,
at base-firmware privilege (single ring, identity-mapped, MPU-only). Its reach is
bounded by **composed mechanisms, not a privilege ring**. The reachability decode
is [`reachability-isolation.md`](reachability-isolation.md) and the runtime cuts
are [`../../runtime/reachability-cuts.md`](../../runtime/reachability-cuts.md).

* **PSUM is structurally unreachable** — no AXI aperture, no pinned NX window, no
  PSUM `ARG_LOCATION` enum (`{INVALID, SBUF, HBM}` only). The compiler verifier
  enforces "all args/outputs of a customop must be located in SBUF". PSUM is closed
  **at the ABI**, not merely at the aperture. The subtle point:
  **"reachability-of-address ≠ reachability-of-data"** — even where a PSUM address
  decodes, there is **no AXI aperture to read PSUM data**. [HIGH/OBSERVED]
* **The capability gate is composed of two tables:** a **14-region capability
  table** plus the **5-region `neuron_translate` software TLB**. Every framework
  pointer is run through `neuron_translate` before any access; an op **cannot
  manufacture reach** to a region whose SoC base it was not handed. [HIGH/OBSERVED]

| Target | Reachable? | Fence |
|---|---|---|
| own SBUF slice (16 parts) | YES | `---` convention (PRID); shared base |
| other cores' SBUF parts | YES (soft) | `---` **no HW fence** (convention only) |
| own per-core DRAM aperture | YES | `===` HW-disjoint (`9 + 2·cpu_id`) |
| other cores' DRAM aperture | NO | `===` PRID-rebased disjoint SoC slice |
| HBM scratch / tensors | YES | `---` host-granted descriptors + `neuron_translate` |
| PSUM | **NO** | `===` no aperture / window / ARG enum (ABI-closed) |
| another NeuronCore | **NO** | `===` per-FIS remapper + per-tpb aperture |
| privileged sprot / Pacific CSRs | **NO** | `===` unaddressable `PEB` plane + CSR deny-all |

(`===` HW/structural fence; `---` software/convention fence.)

> **NOTE — the real isolation boundary is the NeuronCore.** Two workloads on
> **separate** NeuronCores are HW-isolated (per-FIS remapper CAM + per-tpb aperture
> + per-core DRAM index + host per-NeuronCore context). Two workloads on the **same**
> NeuronCore share the 8 Q7 cores + the SBUF and rely **entirely** on software
> isolation — the same-NeuronCore split is **not** a security boundary.
> [HIGH facts / MED advisory]

---

## 5. The genuine findings, tiered by severity

The threat surface that matters is **co-tenancy**: ops that share a NeuronCore. The
execution model was built for performance in a **single-tenant, trusted-compiler**
model, not for mutual confidentiality between mutually-distrusting ops. Full decode:
[`side-channel-leakage.md`](side-channel-leakage.md).

| Severity | Finding | Why this tier | Primary fix |
|---|---|---|---|
| **HIGH** | **Residual-data disclosure (SBUF / HBM scratch never scrubbed)** | a **direct read** of another workload's leftover bytes — no precise instrument, **crosses cores** (SBUF is convention-partitioned). `dmem_free()` with no `memset`; stage path is "alloc only, no copy" | **M1** (allocator scrub) |
| **MED-1** | **Timing via the shared SBUF arbiter (`CCOUNT`-instrumented)** | requires co-tenancy + a precise instrument; leaks activity **indirectly**. `CCOUNT` is op-readable (§1B QUIRK); the 2-stage arbiter stalls the loser one slot. (No data cache → `DCACHE_SIZE=0`, L298 — no Prime+Probe class) | **M5** (data-oblivious) |
| **MED-2** | **Shared architectural register-file / heap / globstruct residue** | two serialized ops on one core share the vector/scalar register files, FP control (`FS0`/`FS1`), `.globstruct`, the CRT heap/TLS/.bss — **no clear between them**. The CRT issues `memw`/`fsync` but no register scrub | **M3** + **M4** |

**Structural facts** (design properties, not severity-rated bugs): **SF-1 — no
firmware signing** (the root cause that makes the ACTOR-2 co-resident-op threat
*adversarial* rather than benign-leak); **SF-2 — the silent override is a total
*compute*-engine compromise for a host-capable actor but remains *fabric-bounded*
off-core** (it cannot reach the Pacific plane or another NeuronCore, and is
reachable only by the trust root).

**What is already sound** (the non-findings): the PMU / profiler surface is
**not op-readable** (host-armed-over-MMIO / JTAG-only, drained into HBM rings the
host reads; perf counters sit behind `ERACCESS` → an unprivileged read faults
`0x211 PRIV_ERI_ACCESS`) — see
[`profiling-trace-debug-gating.md`](profiling-trace-debug-gating.md); **no data
cache** eliminates the data-cache side-channel class for free; **per-core DRAM is
disjoint**; **dispatch is serialized** (residue channels, not live Prime+Probe);
and the model is **fail-stop**. [HIGH/OBSERVED]

---

## 6. Software-only mitigations

Because the on-core model has **no per-op hardware isolation** (single ring, no
per-op MPU re-key, shared SBUF), **every** mitigation is software, applied by the
host runtime and/or the op CRT. The serialized dispatch (one op at a time per core)
is the leverage point: a scrub at the serialization boundary closes the residue
channels.

| ID | Mitigation | Leverage | Closes |
|---|---|---|---|
| **M1** | **Scrub scratch on the allocator** — `memset(0)` every `MR_TMP_BUF` region + the SBUF aperture at stage time (turn "alloc only" into "alloc + zero"), or scrub on free | **HIGH** | the HIGH residual-data finding (the cleanest fix) |
| **M2** | Op-side zero-before-read discipline — treat all scratch as uninitialized | defense-in-depth | benign self-leak only (not a malicious neighbour) |
| **M3** | Register-file + FP-status scrub at op boundary (CRT zeros `vec`/`gvr`/`wvec`/`valign`/`vbool`/`b32_pr`, resets `FS0`/`FS1`) | MED | MED-2 register residue |
| **M4** | Heap / TLS scrub or per-op arena reset | MED | MED-2 heap residue |
| **M5** | Constant-time / data-oblivious op code for secret-dependent paths (HW cannot prevent the timing channel; only data-oblivious code removes the signal) | MED | MED-1 timing |
| **M6** | **Isolate at NeuronCore granularity** — never co-locate mutually-distrusting tenants on one NeuronCore's Q7 cluster | **HIGH** (the real boundary) | subsumes M3/M4/M5 for the cross-tenant case |
| **M7** | Sign / attest custom-op images (removes "untrusted op on a trusted core" at its root) | root cause | SF-1 — above this layer's scope |

**Mitigation → finding map:** HIGH residual-data → M1 (+ M2, + M6); MED-1 timing →
M5 (+ M6); MED-2 register/heap → M3 + M4 (+ M6); SF-1 no signing → M7 (+ M6).

---

## 7. The one-page model + trust-boundary diagram

### BOOT → RUN → FAULT → RECOVER

* **BOOT.** The fabric is armed by the fenced "Pacific" management Q7 (amzn CAM →
  NSM clear-bypass → tighten user CAM → qos/NTS; CSR bus deny-all except
  `MISC_RAM`); `amzn_remapper` boots fail-closed (the one reset-secure block), NSM
  bypassed, qos off, `user_remapper` fail-open until armed. The compute cores
  release run-stall (`0xFF → 0x00`), fetch the reset vector, arm `VECBASE` + the
  16-entry MPU **once**, run crt1 → main, post `.globstruct[0] = 0x6099CB34` READY;
  the host writes `0x502B2DA1` CLAIM (a spin-mailbox, **not** a CAS, **not**
  attestation).
* **ATTEST (none).** Admission = integrity + structure + version. No key, no
  signature, no measured boot. S2 = unkeyed MD5/SHA-256, optional. Secure Mode off,
  secmon dead in silicon. **Trust root = the host/process boundary.**
* **RUN.** On-core: single ring, identity-mapped, MPU-only (no per-op re-key).
  Off-core: every AXI egress crosses the remapper CAM + bound/txn-len/timeout + NSM.
  PSUM unreachable; another NeuronCore unreachable; the secure plane unaddressable;
  per-core DRAM PRID-disjoint; shared SBUF convention-partitioned (soft).
* **FAULT.** 9 XEA3 EXCCAUSE TYPEs (61 `*_exc` in the ISS) + 37 interrupts (one
  `DispatchVector`, ER `0x122000` controller, polled). Firmware SEQ ErrorHandler:
  FP recoverable, **all else → FATAL halt-spin** + a 16-byte `NEURON_ISA` record
  (subtype `BAD_OPCODE` / `SIGNAL` / `EXCEPTION{Type,Cause}` / `ASSERT`) → notific
  ring + `comp_efd` 2-stage wake.
* **RECOVER.** On-core: **no auto-recover** (except FP) — the engine halts; the host
  drains the record and re-initializes. Off-core: the PCIe isolation SM poisons →
  isolates → drains `{aw,w,ar}` FIFOs → resets iofabric → re-arms, under a critical
  apex IRQ to Pacific; most-severe → scan-dump + clock-stop + SRAM-write-protect
  freeze.

### Trust-boundary diagram

```text
Legend:  ===  HARDWARE / STRUCTURAL boundary (device-enforced, no host trust)
         ---  SOFTWARE / CONVENTION boundary (relies on host-staged image)
         >>>  the load / install data flow (S1..S7)

+---------------------- HOST (x86, libnrt) -- TRUST ROOT ----------------------+
|  ACTOR-3 lives here. Whoever can call libnrt is, BY DESIGN, TRUSTED.         |
|   NEFF >>> S1 container gate (size / major<=2 / feature-trapdoor / arch)     |
|        >>> S2 integrity (UNKEYED MD5/SHA-256, OPTIONAL -- not authentication)|
|        >>> S3 ucode semver + opcode bind (BUILT-IN / USER ExtISA)           |
|        >>> S4 ELF prelink: xtlib_verify_magic 0x9b6d40 / validate_dynamic_  |
|                load 0x9b71f0 / prelink_relocate_lib 0x9b6160 (type-5 reloc) |
|        >>> S5 nrt_set_pool_eng_ucode - SILENT, UNAUTHENTICATED OVERRIDE --+  |
|              (the trust gap; install = write_padded 0x473eb0 -> MMIO,     |  |
|               NOT BAR0 DMA. BAR4 = the zero-copy WEIGHT path only.)       |  |
+--------------------------------------------------------------------------+--+
      | MMIO write (write_padded -> al_mem_write_buf 0x265990)             |
      v                                                                    v
=========================== SoC FABRIC PERIMETER (Layer A) =====================
=  Every AXI txn crosses here. ARMED by the fenced "Pacific" MGMT Q7          =
=  (reset-default LAX -> armed secure). amzn fail-CLOSED at reset.            =
=   user_remapper (GUEST, APB_IO)   ===  amzn_remapper (SECURE, PEB_APB_IO)   =
=     fail-OPEN at reset, armed->closed     fail-CLOSED (the ONE reset-secure)=
=   NSM watchdog (bypassed->armed) | qos/NTS poison (deadbeef 8/1/0)          =
=   sprot CSR bus: DENY-ALL except MISC_RAM  <== HOST CANNOT REACH ARMING CSRs=
=============+=====================================+===========================
      compute plane (APB_IO, user)         secure plane (PEB_APB_IO) -- Pacific
            |                                     |  (unaddressable by op)
            v                                     v
============ COMPUTE NeuronCore (Layer B) =======    +-------------------------+
= S6 device loader: total_cpus{1,8}/start_sym/  =    | "PACIFIC" MGMT Q7:      |
=    library_size  (NO ELF/reloc/hash re-check) =    |  arms the perimeter;    |
= S7 run-stall 0xFF->0x00 -> reset vector ->    =    |  receives critical apex |
=    VECBASE + 16-entry MPU (once) -> crt1 ->   =    |  IRQs (NSM/deny cause   |
=    main; .globstruct[0]=0x6099CB34 READY ->   =    |  111); DISTINCT CORE.   |
=    host writes 0x502B2DA1 CLAIM               =    +-------------------------+
=  8 Q7 POOL cores (SPMD, single ring, MPU-only):=   PSUM === UNREACHABLE
=   === per-core DRAM disjoint (9+2*cpu_id)     =      (no aperture/window/ARG)
=   --- SBUF shared, CONVENTION-partitioned <---=---- HIGH residual-data leak +
=       (HIGH residual leak; timing via arbiter)=      MED-1 timing cross cores
=   --- co-located ops: NO on-core fence        =   another NeuronCore ===
=       (MED-2 register/heap/globstruct residue)=     unreachable (per-tpb
=   neuron_translate (5-region TLB) + 14-region =      aperture + remapper CAM)
=    capability table: name only granted regions=
=   CAUSE 1/3/4 -> SEQ 0x1a64 ; 0/2/5/6/7/8 ->  =   -- all on-core violations
=    XTOS 0x1c2ac ; ALL FATAL except FP -> halt =      FAIL-STOP -> 16B record
=================================================      -> notific ring + comp_efd
```

### What protects what — the honest verdict

| Class | Mechanism | Trust assumption |
|---|---|---|
| **Protected by hardware** (no host trust) | fabric perimeter vs a misbehaving PCIe peer; compute → management escalation; cross-NeuronCore reach; PSUM access; per-core DRAM disjointness; on-core out-of-rights → MPU fail-stop; malformed AXI → NSM poison | **none** |
| **Protected by integrity / structure only** | corruption (unkeyed hash); forward-incompatibility (feature trapdoor); structural malformation (ELF / reloc / core-count) | catches **accidents**, not a self-consistent forgery |
| **Not protected — relies on host trust** | image **authenticity** (no signing, no crypto root); the install seam against a host-capable actor (the silent override); inter-op isolation **within** a NeuronCore; the perimeter's guest half until armed | **host is trusted** |
| **Out of the threat model** | a malicious host process that can call `libnrt` | **it is the trust root** |

> **The single honest takeaway.** GPSIMD has a hardware **access-control** trust
> boundary (the remapper twins, armed by a fenced management core) but **no image
> authentication** trust boundary (no crypto root of trust anywhere on the load
> path; Cadence Secure Mode disabled in silicon). It is **fabric-isolated,
> fail-stop, and host-rooted** — secure at the perimeter, integrity-checked at
> admission, unsandboxed within a NeuronCore, and unauthenticated at the firmware
> image. The host/process boundary is the trust root, **by design** — and that
> must be an **explicit** assumption in any GPSIMD deployment's threat model.

---

## 8. Provenance ledger

**HIGH / OBSERVED (re-verified this pass against a shipped artifact):**

* `xtlib_verify_magic 0x9b6d40` / `validate_dynamic_load 0x9b71f0` /
  `prelink_relocate_lib 0x9b6160`; zero crypto-auth dynsyms; NEEDED = `libc.so.6`
  only — `nm`/`readelf` on `libnrtucode_internal.so`.
* `XCHAL_HAVE_SECURE=0` (L283), `XCHAL_MMU_RINGS=1` (L794),
  `XCHAL_HAVE_IDENTITY_MAP=1` (L783), `XCHAL_HAVE_PTP_MMU=0` (L787),
  `XCHAL_HAVE_MPU=1`/`XCHAL_MPU_ENTRIES=16` (L800/801), `XCHAL_HAVE_KSL=1`/`ISL=1`
  (L710/711), `XCHAL_HAVE_XEA2=0`/`XEA3=1` (L725/726), `XCHAL_EXCCAUSE_NUM=9`
  (L730), `XCHAL_NUM_INTERRUPTS=37`/`EXTINTERRUPTS=25`/`INTLEVELS=7`
  (L457/459/460), `XCHAL_HAVE_CCOUNT=1` (L455), `XCHAL_DCACHE_SIZE=0` (L298) —
  `ncore2gp` `core-isa.h`.
* `nm libcas-core.so | rg -c '_exc$' = 61` — the ISS fault vocabulary.
* boot sentinels `0x6099CB34` (READY) / `0x502B2DA1` (CLAIM) present in
  `libnrtucode_internal.so` (byte scan).

**HIGH / CARRIED (decoded by a named sibling; cited, re-confirmed by spot-check):**
the 7-stage load chain + silent override + actor model
([`trust-chain-threat-model.md`](trust-chain-threat-model.md)); the byte-exact
remapper / NSM / qos-NTS perimeter + the deadbeef 8/1/0 + errtrig Cayman 962
([`soc-fabric-perimeter.md`](soc-fabric-perimeter.md)); the reset-default-LAX →
armed transition + recovery SM
([`boot-arming-fault-recovery.md`](boot-arming-fault-recovery.md)); the partial
SEQ override (CAUSE 1/3/4 → `0x1a64`; 0/2/5/6/7/8 → `0x1c2ac`) + the 16-byte
record ([`boot-fault-overview.md`](boot-fault-overview.md)); PSUM / plane / DRAM /
SBUF reachability ([`reachability-isolation.md`](reachability-isolation.md)); the
four leakage channels + tiered findings + M1..M7
([`side-channel-leakage.md`](side-channel-leakage.md)); the host-only PMU gating
([`profiling-trace-debug-gating.md`](profiling-trace-debug-gating.md)).

**MED / INFERRED:** "no cryptographic root of trust" / "the host is the trust
root" / "co-located ops are not isolated" — all follow from the observed
structural facts. The SBUF "convention, not fence" reading and the
"residue visible to op B" reading are inherited at their original MED tag. The
arming **order** between the observed CSR writes is inferred from steady-state
dependencies.

**LOW / OPEN:** the apex-pending-bit → Pacific GIC vector hop (firmware/HW-owned,
not register-encoded); the exact host-side read binding of `UR#0x15` vs the notify
ring (write OBSERVED, host consumer out of scope); the default value of the S2
verify flag across all `nrt_load` callers.

**Maverick (NC-v5) interiors are INFERRED** from the Sunda / Cayman / Mariana
pattern unless a Maverick artifact is cited directly.
