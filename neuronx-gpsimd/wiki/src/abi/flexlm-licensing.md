# FlexLM Licensing Gate

This page documents the **license-gating mechanism** baked into the shipped
`XtensaTools` tree: which binaries actually perform a license checkout, the FlexNet
(Flexera FlexLM) client that does it, the FEATURE names each engine checks out, the
checkout flow, the failure behavior, and the bundled-vs-deployed license posture. The
central, reimplementation-relevant fact is the **split**: the *value-producing* paths
(compile/link with clang, the ISS' functional value-only modes) are **license-free**,
while the *cycle/fault* paths (the cycle-accurate ISS engine and the TIE/config-gen
engine) are **license-gated**.

This is **white-hat interoperability documentation** of the gate as observed in the
shipped, redistributable tree (lawful RE under DMCA 17 U.S.C. §1201(f)). It describes
*what* gates and *why* the value path differs from the cycle path. It does **not**
derive, supply, or suggest any bypass, key, hostid spoof, or patch.

Everything below is anchored to shipped artifacts read **verbatim this session** — an
ELF `.dynsym` entry (`nm -D`), an embedded `strings` token, a license-file body, a
banner. No vendor source snapshot is consulted or quoted; all output reads as derived
from binary/file analysis of the shipped tree only.

| Artifact | Path (under `…/XtensaTools/`) | Role |
|---|---|---|
| FlexLM client | `Tools/lib/tenlp.so` (1 155 296 B, BuildID `c26f7cfd…813c`, Apr 4 2021) | Tensilica's wrapper around the statically-linked Flexera `liblmgr.a`; `dlopen`'d at runtime |
| Dlopen shim | `Tools/lib/libtenlpw.a` (7 924 B, Apr 4 2021) | resolves `tenlp.so` via `dlopen`/`dlsym`; the linked-in client shim |
| API header | `Tools/include/tenlp.h` (71 lines) | the checkout API + policy bytes + error codes |
| Bundled license | `Tools/lic/license.dat` (1 116 B, sha256 `73da37e7…06ad4`, Apr 4 2021) | **placeholder** — comment-only, no keys |

Claims are tagged `[HIGH/OBSERVED]` (verbatim ELF/string/file bytes read this session),
`[MED/INFERRED]` (reasoning over those bytes / FlexLM convention), or `[CARRIED]` (from a
cross-referenced lane). Source report: **SX-ABI-16**. The `|` glyph in tables is written
`\|`. Cross-links: the gated tools and their place in the build are owned by
[Build Flow](build-flow.md) and [Build → Codegen](build-custom-op-codegen.md); the
version pin is owned by [Toolchain Versions](../reference/toolchain-versions.md).

---

## 1. The FlexNet client and its version pin

The Cadence/Tensilica Xtensa Tools license through **FLEXlm** (Flexera FlexNet
Publisher). The entire FlexLM client is one shipped library, `Tools/lib/tenlp.so`, whose
header comment names it exactly:

> `/* Tensilica's interface to FlexLM licensing */` — `Tools/include/tenlp.h:1-2`. `[HIGH/OBSERVED]`

`tenlp.so` is a stripped x86-64 ELF shared object that statically links Flexera's
`liblmgr.a`. Its embedded build banner pins the FlexNet version exactly:

```
@(#) FlexNet Licensing v11.15.1.0 build 225974 (ipv6) x64_lsb (liblmgr.a),
     Copyright (c) 1988-2018 Flexera. All Rights Reserved.
```
`[HIGH/OBSERVED — strings(tenlp.so)]`

So the gate is **Flexera FlexNet Publisher 11.15.1.0**, the IPv6 x64 Linux `liblmgr.a`,
statically linked into a client dated **Apr 4 2021**. The `(c) …-2018 Flexera` plus the
file date bracket the build. `tenlp.so`'s only `NEEDED` libraries are `libpthread.so.0`
and `libc.so.6` (`readelf -d`) — i.e. all of FlexLM is *inside* this one file. `[HIGH/OBSERVED]`

Because `liblmgr.a` is **statically** linked, the FlexLM client API (`lc_init`,
`lc_new_job`, `lc_checkout` …) is **not** in the dynamic symbol table. It survives only
as string fragments inside diagnostics, e.g. `"No key data supplied in call to
lc_new_job() or lc_init()."` and `"Unknown VENDORCODE struct type passed to lc_new_job()
or lc_init()."`. The only exported (`nm -D`) symbols are Tensilica's eight `tenlp_*`
entry points plus `tenlps_authcode`, all versioned `@@VERS_1.1`. `[HIGH/OBSERVED]`

> **NOTE.** No FlexLM **daemon** or **admin** binary ships anywhere in the tree — no
> `lmgrd`, `lmutil`, `lmstat`, `lmhostid`, `lmdown`, and no standalone `xtensad` vendor
> daemon (a `fd --no-ignore` sweep returns nothing). The shipped tree is a license
> **client only**; any *served* (`lmgrd` + `xtensad`) deployment, if used, is external.
> The vendor-daemon name `xtensad` survives **only** as the example token in the
> placeholder `license.dat` (§6). `[HIGH/OBSERVED]`

---

## 2. Which tools gate — the per-binary FlexLM map

**The rule.** A binary gates iff it carries `tenlp_checkout` — it links the
`libtenlpw.a` shim, `dlopen`s `tenlp.so`, and performs a checkout. The authoritative map
is `rg --no-ignore -a -l 'tenlp_checkout'` over the whole tree, cross-checked with
per-binary `strings`/`nm -D`. `[HIGH/OBSERVED]`

### 2.1 The consumers that gate

| Consumer (under `…/XtensaTools/`) | FEATURE checked out | Role |
|---|---|---|
| `lib/iss*/libsimxtcore.{so,a}` — **11** per-host-compiler ISS builds | `XTENSA_ISS_BASE` (alias `XT_ISS_BASE`) | the ISS engine loaded by `xt-run` (cycle-accurate / cas-fiss sim) |
| `lib/extend.so` | `XTENSA_XCC_TIE` (alias `XT_XCC_TIE`), plus `XT_XCC_FUSA` | the legacy XCC / TIE extension engine |
| `TIE/bin-i686-Linux/tcgen` | shares the XCC/TIE checkout path | the TIE source compiler (config / TDK generation) |

The eleven `libsimxtcore.so` builds are `iss`, `iss-clang-10`, and `iss-GCC-{4.8, 6.2,
6.3, 6.3-XCM, 7.3, 9.2, 9.3, 10.2, 11.2}` — one per *host* compiler used to build the
simulator (the `.a` static variants gate too). All carry `XTENSA_ISS_BASE` /
`XT_ISS_BASE` and the license-init class `ISSArchLicense`. `[HIGH/OBSERVED]`

> **NOTE — the gate is in the loaded engine, not the linkage record.** Neither
> `libsimxtcore.so` nor `extend.so` lists `tenlp.so` as a `NEEDED` library. Instead each
> **statically links the `libtenlpw.a` shim** (it re-exports `tenlp_checkout`,
> `tenlp_init`, … as its own `T` symbols) and imports `dlopen@GLIBC` — so `tenlp.so` is
> resolved by name at *runtime*, not bound at link time. `[HIGH/OBSERVED]`

### 2.2 The binaries that do **not** gate

Verified `tenlp_checkout` / `FlexNet Licensing v` / `XTENSA_ISS_BASE` hit count = **0**
on each, this session:

* **All GNU binutils** — `xtensa-elf-{objdump, nm, ld, as, ar, objcopy, readelf, strip,
  addr2line, c++filt, size, strings, gprof, ranlib}`. GPL code cannot legally embed
  proprietary FlexLM, and none does. `[HIGH/OBSERVED]`
* **The thin `xt-*` driver binaries** — `xt-clang`, `xt-clang++`, `xt-run`,
  `xt-config`, `xt-gdb`, and the `xt-{as,ld,nm,…}` wrappers (0 hits each). These are
  x86-64 ELF dispatch front-ends; the gate lives in the *engine each one loads* (`xt-run`
  → `libsimxtcore.so`). `[HIGH/OBSERVED]`
* **The entire clang/LLVM-10 stack** — `clang-10`, `libLLVMXtensaCodeGen.so.10`,
  `libLLVMCodeGen.so.10`, `libLLVMXtensaDesc.so.10` (0 hits each). `[HIGH/OBSERVED]`

> **CORRECTION (raises an SX-ABI-16 §3.3 claim).** SX-ABI-16 reports
> `XTENSA_INSTR_BASE` / `XTENSA_INTRN_BASE` as **FlexLM FEATURE tokens** present in
> `libLLVMCodeGen.so.10` and `llvm-xtensa-config-gen`. That is **incorrect** —
> see §3.3. Those libraries call `tenlp_checkout` **zero** times; the substrings are
> LLVM instruction-property enum names, not license features.

---

## 3. The FEATURE names checked out

A FlexLM FEATURE token is the string passed as the `feat` argument to `tenlp_checkout`
(§4). Only **two** engines really check out, so only **two** FEATURE families are real
in this tree.

**3.1 — ISS (`libsimxtcore`).** FEATURE **`XTENSA_ISS_BASE`** (alias **`XT_ISS_BASE`**),
both embedded verbatim, alongside the license-init class **`ISSArchLicense`**. `[HIGH/OBSERVED]`

**3.2 — XCC / TIE engine (`extend.so`, `tcgen`).** FEATURE **`XTENSA_XCC_TIE`** (alias
**`XT_XCC_TIE`**), plus **`XT_XCC_FUSA`** (the functional-safety XCC feature). `extend.so`
and `tcgen` share the `License checkout failed` / `License initialization failed` path
(§5). `[HIGH/OBSERVED]`

**3.3 — the `INSTR_BASE` / `INTRN_BASE` strings are *not* features.** The only forms of
these substrings present in the corpus are `XTENSA_INSTR_BASE_DISP_IMM`,
`XTENSA_INSTR_BASE_DISP_REG` (in `llvm-xtensa-config-gen`) and `XTENSA_INTRN_BASE_DISP_IMM`,
`XTENSA_INTRN_BASE_DISP_REG` (in `libLLVMCodeGen.so.10`). The shipped header context
proves they are **C++ enum members** of `XtensaLdStKind` /
`XtensaMemIntrnKind` — load/store addressing-mode classifiers (base + displacement,
immediate vs register; e.g. `IVP_LVNX16_I` / `IVP_LVNX16_X`):

```cpp
// Tensilica/TensilicaIntrnProperty.h
XTENSA_INTRN_BASE_DISP_IMM, // eg: IVP_LVNX16_I
XTENSA_INTRN_BASE_DISP_REG, // eg: IVP_LVNX16_X
```

The bare tokens `XTENSA_INSTR_BASE` / `XTENSA_INTRN_BASE` (no `_DISP_*` suffix) **never
appear** in the tree, and no `tenlp_checkout` reference sits near them. They are LLVM
codegen properties, not FlexLM FEATUREs. `[HIGH/OBSERVED — exact-token + header context]`

**3.4 — the placeholder example FEATURE.** `license.dat` documents the FEATURE-line
*grammar* with one commented example:

```
FEATURE XTENSA_EVAL xtensad 4.0 30-Apr-2000 0 454780A5EF33 HOSTID=0070047a977a
```

i.e. feature `XTENSA_EVAL`, vendor-daemon `xtensad`, version `4.0`, an expiry, count `0`
(uncounted / node-locked), a key, and `HOSTID=<ether>`. This pins the vendor-daemon name
and the node-locked key shape — but ships with no real key (§6). `[HIGH/OBSERVED]`

---

## 4. The checkout flow

`dlopen` → `tenlp_init` → `tenlp_checkout` → `tenlp_heartbeat` → `tenlp_checkin`.

### 4.1 The dynamic-load shim (`libtenlpw.a`)

A gated engine links `libtenlpw.a`, which holds **nine** function pointers —
`ptenlp_{checkout, checkin, heartbeat, init, errstring, warning, perror, pwarn}` plus
`ptenlps_authcode`. On first use it builds the client path and `dlopen`s it:

* `getenv("XTENSA_TOOLS")` → the tools root,
* `getenv("TENLP_DLL")` → the client basename (default `tenlp`),
* `sprintf("%s/lib/%s.so", root, dll)` → `…/XtensaTools/Tools/lib/tenlp.so`,
* `dlopen()` it, `dlsym()` the entry points; the default license path it constructs is
  `"%s/lic/license.dat"`.

On `dlopen`/`dlsym` failure it prints `Error: %s: Cannot load Tensilica Licensing
library` and returns `TENLP_NODLL (-799402)`. `[HIGH/OBSERVED — tenlpw.o strings]`

> **CORRECTION (refines SX-ABI-16 §4.1).** That report counts the shim's pointer table
> as **eight** entries. The `libtenlpw.a` archive embeds **nine** `ptenlp_*` symbols —
> the eighth-vs-ninth discrepancy is `ptenlp_init`, which the §4.1 enumeration omitted.
> The exported-symbol set in `libsimxtcore.so` confirms `tenlp_init` is present (`T
> tenlp_init`). The flow is otherwise as described.

### 4.2 The API contract (`Tools/include/tenlp.h`, verbatim)

```c
int  tenlp_init(void);
int  tenlp_checkout(int policy, char *feat, char *vers,
                    int count, char *path, TENLP_HANDLE **h);
void tenlp_checkin (TENLP_HANDLE *h);
int  tenlp_heartbeat(TENLP_HANDLE *h, int *nr, int nm);
char*tenlp_errstring(TENLP_HANDLE *h);
char*tenlp_warning  (TENLP_HANDLE *h);
void tenlp_perror   (TENLP_HANDLE *h, char *s);
void tenlp_pwarn    (TENLP_HANDLE *h, char *s);
```

`policy` = the checkout mode (§4.3); `feat` = the FEATURE name (§3); `vers` = the feature
version (`"4.0"`-style); `count` = number of licenses; `path` = a license-path override;
`h` = the returned handle (`NULL` on failure). `[HIGH/OBSERVED]`

### 4.3 The policy byte + modifiers (`tenlp.h` `#define`s)

These are Tensilica's names over the FlexLM `lc_checkout()` policy mask:

| Field | Token | Value | Meaning |
|---|---|---|---|
| mode (low byte, mask `0xff`) | `LM_RESTRICTIVE` | `0x1` | fail if unavailable |
| | `LM_QUEUE` | `0x2` | block / queue |
| | `LM_FAILSAFE` | `0x3` | continue on failure |
| | `LM_LENIENT` | `0x4` | lenient |
| modifier bits (next 3 bytes) | `LM_MANUAL_HEARTBEAT` | `0x100` | caller drives the heartbeat |
| | `LM_RETRY_RESTRICTIVE` | `0x200` | retry restrictive |
| | `LM_ALLOW_FLEXLMD` | `0x400` | allow served `flexlmd` |
| | `LM_CHECK_BADDATE` | `0x800` | reject clock-back |
| | `LM_FLEXLOCK` | `0x1000` | FLEXLOCK trusted storage |

`[HIGH/OBSERVED — tenlp.h:43-58]`

### 4.4 License-source resolution

The underlying `liblmgr` resolves the license source in standard FlexLM precedence; the
strings present in `tenlp.so` are `LM_LICENSE_FILE` (the generic search path), the
per-vendor `%s_LICENSE_FILE` template (for `xtensad` that is the conventional
`XTENSAD_LICENSE_FILE`), the explicit `path` arg to `tenlp_checkout`, and finally the
default `"%s/lic/license.dat"` (the bundled placeholder). `[the env tokens HIGH/OBSERVED;
the precedence ordering MED/INFERRED from FlexLM convention]`

The **customer build flow pins this**: `build_custom_op.py:19-20` forces
`LM_LICENSE_FILE`, **when unset**, to
`/opt/aws/neuron/gpsimd/tools/licenses/amzn_vq7_us_582883.out` (and echoes it at `:377`),
so the deployed `.out` wins over the empty bundled `license.dat`. `[HIGH/OBSERVED]`

### 4.5 Hostid resolution — the cloud-licensing core

At checkout the client computes the host's FlexLM hostid via a full FlexNet 11.15
virtualization/cloud detector. Embedded strings: `AmazonEC2 detected`, `VMWare detected`,
`QEMU detected`, `Hyper-V detected`, `XEN detected`, `VirtualBox detected`, `Physical
machine detected`, plus `CPUID Hypervisor Detection positive result`. On EC2 it derives
an **Amazon composite hostid** from `AMZN_AMI` / `AMZN_IID` / `AMZN_EIP` (AMI id, instance
id, elastic IP — each also overridable via the matching `AMZN_AMI=` / `AMZN_IID=` /
`AMZN_EIP=` env value), fetching them over HTTP (`GET %s HTTP/1.0`, `Received HTTP
response body`). Diagnostics: `Running Amazon EC2 Mechanism`, `Amazon EC2 Mechanism
positve result` [sic], `Failed to obtain AMZN hostid.`, `Amzn hostid error 002_1 = %d`,
`checkout.hostId=%s`, plus standard composite machinery (`Composite hostid not
initialized.`). `[hostid machinery HIGH/OBSERVED; "fetch from the EC2 instance-metadata
service" MED/INFERRED from the HTTP-GET + AMZN_* + EC2-detect co-location]`

### 4.6 Heartbeat / reconnect

For a **served** (counted) feature the client maintains the FlexLM heartbeat
(`FLEX_MSG_HEARTBEAT`, `HEARTBEAT_INTERVAL`) via `tenlp_heartbeat()`; on a dropped server
it reports `Lost license, cannot re-connect` / `Failed to get all requested license(s) in
reconnection.`. For a **node-locked** `.out` (§6) no server connection or heartbeat is
needed. `[HIGH/OBSERVED]`

---

## 5. Failure behavior

**5.1 — `tenlp`-level errors** (`tenlp.h`, verbatim): `TENLP_BADAUTH (-799401)`
"Tensilica Licensing authentication error"; `TENLP_NODLL (-799402)` "Cannot load
Tensilica Licensing library". The shim prints these to `stderr` (`Error: %s: …`) and
aborts the licensed operation. `[HIGH/OBSERVED]`

**5.2 — FlexLM-level diagnostics** (surfaced through `tenlp_errstring` / `tenlp_perror`;
all strings in `tenlp.so`): `FlexNet Licensing error:`, `Cannot connect to license server
system.`, `No such feature exists.`, `Feature has expired.`, `Invalid host.`, `Cannot
find license file.`, `The desired vendor daemon is down.`, formatted with the FlexLM
error-code machinery (`INVALID FlexNet Licensing error code`). `[HIGH/OBSERVED]`

**5.3 — per-engine path.**

* **ISS (`libsimxtcore`)** prints `License initialization failed` / `Unable to get
  license` and exits the simulation. It offers a retry/grace knob:

  ```
  --wait_for_license=n makes ISS retry failed license checkouts. ISS will try
              n times waiting 1 min between tries.
  ```

  plus env `XTENSA_LICENSE_RETRIES` (retry count) and `XTENSA_PREFER_LICENSE` (which
  feature/source to prefer). With no `--wait_for_license`, the default is fail-on-first-
  miss. `[help text HIGH/OBSERVED; "exits on default-fail" MED/INFERRED from the message
  + the explicit opt-in retry semantics]`
* **XCC / TIE (`extend.so`, `tcgen`)** print `License checkout failed` / `License
  initialization failed` and abort the compile/gen. `[HIGH/OBSERVED]`

**5.4 — offline grace / BORROW.** The client compiles in FlexLM BORROW (`LM_BORROW`,
`FLEXLM_BORROWFILE`, `.flexlmborrow`, `BORROW period expired.`, `MAX_BORROW_HOURS`) and
trusted-storage support, so an offline grace period is a *capability* of the client.
Whether it is *used* depends on the deployed `.out`'s FEATURE/INCREMENT attributes — not
determinable from the empty bundled `license.dat`. `[BORROW capability HIGH/OBSERVED;
"used here" LOW/UNKNOWN]`

---

## 6. The license posture — bundled placeholder vs deployed node-lock

**6.1 — the bundled `license.dat` is an empty placeholder.** `Tools/lic/license.dat`
(1 116 B, sha256 `73da37e7b0f118b7ae354512ac790cb7522b6d5456e0f481ff0b1bb0b9c06ad4`)
contains **only comment lines** — every non-blank line begins with `#`; no
`FEATURE`/`INCREMENT`/`SERVER` line is present. Its text explains the three FlexLM setups
(network server via `LM_LICENSE_FILE`; node-locked keys pasted into this file; the eval
example FEATURE line) and tells the user to *"append your keys at the end"* — but ships
with none. The bundled tree carries **no working entitlement** on its own. `[HIGH/OBSERVED]`

**6.2 — the real entitlement is a deployed node-locked `.out`.** The customer flow
supplies it out-of-band via `LM_LICENSE_FILE` →
`/opt/aws/neuron/gpsimd/tools/licenses/amzn_vq7_us_582883.out` (`build_custom_op.py:20`).
Token reading: `amzn` = Amazon (the licensee), `vq7` = Vision-Q7 (the licensed core
class, matching `ncore2gp`'s `XCHAL_VISION_TYPE=7`), `us` = region, `582883` = a license /
order id. This file is **not** inside the shipped tools tarball — it lives under the
deployed `/opt/aws/neuron` install. `[path HIGH/OBSERVED; the token reading MED/INFERRED
but cross-consistent with the vq7 core + amzn licensee]`

**6.3 — node-locked vs served: the client supports both; this deployment is
node-locked-to-EC2.** `tenlp.so` carries the node-locked path (uncounted features:
`Hostid required for uncounted feature`, `FLOAT_OK only valid with node-locked license`)
*and* the served path (counted features + heartbeat, §4.6). Given (a) no `lmgrd`/`xtensad`
daemon ships (§1), (b) the Amazon-EC2 composite-hostid mechanism (§4.5), and (c) the
`.out` naming (a node-locked fulfillment file, not a server `port@host` pointer), the
posture is a **node-lock bound to the EC2 instance's composite hostid** — the
cloud-native equivalent of an ethernet-MAC node-lock. A served `lmgrd` is *possible* but
is not what this tree is set up for. `[both-supported HIGH/OBSERVED; "node-locked-to-EC2
here" HIGH/INFERRED from §1 + §4.5 + §6.2 convergence]`

---

## 7. The value-free vs cycle-gated split (and where the build flow stands)

The single most useful interop fact: **not every Xtensa run needs a license.** The split
follows the per-binary gate map (§2).

| Path | What runs | Gated? |
|---|---|---|
| **Compile / link** | `xt-clang++` → `clang-10` → GNU `ld` → `xt-pkg-loadlib` | **No** — clang/LLVM/binutils/packager carry 0 `tenlp_checkout`. `[HIGH/OBSERVED]` |
| **ISS — functional / value path** | the simulator's value-only modes | license-**free** in the value path; the checkout is the *cycle-accurate / timing* path's `XTENSA_ISS_BASE` (Runnable-ISS infra, Part 14 — not yet authored). `[CARRIED]` |
| **ISS — cycle / fault path** | `xt-run` → `libsimxtcore` cycle-accurate engine | **Yes** — `XTENSA_ISS_BASE`. `[HIGH/OBSERVED]` |
| **TIE / config regeneration** | `extend.so`, `tcgen` | **Yes** — `XTENSA_XCC_TIE` / `XT_XCC_FUSA`. `[HIGH/OBSERVED]` |

This means the **per-kernel customer build does not check out a Cadence license at
compile/link time** — clang is ungated. The `LM_LICENSE_FILE` that `build_custom_op.py`
exports at import (§4.4) is an *environment it sets for any gated tool that may later
run* (an ISS run, or a config regeneration), not something `clang` itself consumes.

> **CORRECTION (relative to [Build Flow](build-flow.md)).** The build-flow page states
> *"the compiler front-end is FlexLM-gated, so a rebuild needs a valid `LM_LICENSE_FILE`."*
> Per the per-binary evidence here, `clang-10` and the `xt-clang++` driver carry **zero**
> FlexLM bytes — the *compiler front-end itself does not check out*. The accurate
> statement is: the wrapper **exports** `LM_LICENSE_FILE` so that the *gated engines*
> (ISS / TIE) find an entitlement *if* the flow invokes them; the compile/link step is
> license-free. The output ELF carrying no license is consistent with this — there is
> simply no checkout on the clang path to begin with. `[HIGH/OBSERVED — clang 0-hit;
> build-flow phrasing CORRECTED here]`

---

## 8. Version axis — one client, version-orthogonal

The entire FlexLM mechanism is a **single-file property**: one `tenlp.so` (+
`libtenlpw.a` + `tenlp.h`), at FlexNet 11.15.1.0, dated Apr 4 2021. Every gated consumer
(the 11 per-host-compiler `libsimxtcore` builds, `extend.so`, `tcgen`) `dlopen`s/links the
**same** `tenlp.so`. So within this shipped tree there is no "14.09 FlexLM set vs some
other FlexLM set" to diff — the FlexLM client is **version-orthogonal** to the XtensaTools
`14.09` / `RI-2022.9` release stamp. `[HIGH/OBSERVED]`

The XtensaTools release the gate belongs to is **14.09 / RI-2022.9** — the generic tools
(`libsimxtcore.so`, `extend.so`) are the Jun 29 2022 `RI-2022.9` build, with the Apr 4
2021 `tenlp.so` carried alongside as the bundled client. See
[Toolchain Versions](../reference/toolchain-versions.md) for the `14.09 = RI-2022.9 =
1409000` pin. `[HIGH/OBSERVED]`

> **NOTE.** A separate `clang-15` / `15.05` build appears elsewhere in this corpus (the
> per-generation ext-ISA runtime), but it is **not** part of the shipped customer
> XtensaTools and shares no binary with this FlexLM client. It introduces no second
> FlexLM client into this tree; any "does the 15.05 toolchain gate differently?" question
> concerns a build not present here. `[HIGH/OBSERVED for this tree]`

---

## 9. Confidence ledger

* **HIGH / OBSERVED** — the FlexLM client `= tenlp.so` (FlexNet v11.15.1.0 banner); the
  no-daemon sweep; the gate-bearing consumers (`tenlp_checkout` via `rg -a -l` +
  per-engine messages: 11 ISS `libsimxtcore` + `extend.so` + `tcgen`); the ungated
  binutils / clang / LLVM / `xt-*` drivers (0-hit per-binary); the FEATURE names
  `XTENSA_ISS_BASE` / `XT_ISS_BASE` / `XTENSA_XCC_TIE` / `XT_XCC_TIE` / `XT_XCC_FUSA`; the
  checkout API + policy bytes + error codes (`tenlp.h` verbatim); the dlopen shim flow
  (`tenlpw.o` strings); the Amazon-EC2 composite-hostid machinery; the empty placeholder
  `license.dat` (sha256-verified); the ISS `--wait_for_license` retry knob; the
  `LM_LICENSE_FILE` pin (`build_custom_op.py:19-20,377`).
* **MED / INFERRED** — the env-var precedence ordering; "EC2 instance-metadata fetch";
  "ISS exits on default-fail"; the `amzn/vq7/us/582883` token reading of the `.out`.
* **LOW / UNKNOWN** — whether BORROW / offline-grace is actually enabled (depends on the
  deployed `.out`, which is not in the shipped tree).
* **CORRECTIONS issued here** — (1) `XTENSA_INSTR_BASE` / `XTENSA_INTRN_BASE` are **not**
  FlexLM features (they are `_DISP_IMM`/`_DISP_REG` LLVM enum members); (2) the
  `libtenlpw.a` pointer table is **nine** entries, not eight (`ptenlp_init` was missing
  from the SX-ABI-16 list); (3) the build-flow page's "compiler front-end is
  FlexLM-gated" is refined — `clang-10` is ungated, the wrapper merely *exports*
  `LM_LICENSE_FILE` for the gated ISS/TIE engines.

This page documents the **gating mechanism only**. It does not derive, suggest, or supply
any bypass, key, hostid spoof, or patch.
