# Firmware Trust Chain + Threat Model

This page is the **white-hat defensive synthesis** of the GPSIMD firmware-load
trust chain: it walks a custom-op / NEFF image from the moment the host runtime
stages it, through every check the runtime and device libraries apply, to the
point the eight Cayman / Vision-Q7 pool cores begin executing it — and at **each
step states exactly what is validated versus what is not**. It then assembles
those gaps into a **threat model**: what a malicious NEFF or custom op could
attempt, what the silicon perimeter actually *stops*, and what *relies on the
host being trusted*. The goal is to let a reimplementer or a defender reason
about the integrity boundary precisely, without over- or under-trusting any one
mechanism.

The single most important fact, stated once and carried through every section:

> **KEYSTONE — there is no cryptographic root of trust on the GPSIMD load path.**
> The chain is **integrity-rooted, not key-rooted**. The only digests anywhere on
> the path are an **unkeyed MD5 / SHA-256** over the container payload (corruption
> detection) and a small set of **plain-version comparisons** (compatibility
> gating). There is **no signature, no MAC, no certificate, no attestation, no
> measured/secure boot, no TPM** — confirmed by the *absence* of any
> `rsa|ecdsa|ed25519|aes|hmac|verify_sig|pubkey` primitive in either device
> library (`nm libnrtucode_internal.so` → 0 hits) and by the runtime carrying
> **zero** `libcrypto`/`libssl` `DT_NEEDED` edge. The device is **armed, not
> default-secure**, and it **fails stop** rather than fails open. `[HIGH ·
> OBSERVED by absence]`

Two companion pages own the steady-state halves of this model and are
**authoritative** for their register byte-detail; this page draws the trust chain
*through* them and does not re-table their fields:

* [The SoC-Fabric Perimeter](soc-fabric-perimeter.md) (#947) — the silicon
  AXI-edge firewall (remapper / NSM / NTS) that contains a *running* image.
* [Boot-Arming + Device Fault Recovery](boot-arming-fault-recovery.md) (#948) —
  the sequence that *arms* that firewall and the fail-stop fault machine.

Forward references (stubs at time of writing — cross-link, do not duplicate):
the on-core isolation map [Reachability / Isolation](reachability-isolation.md)
(#950); the security capstone [Security Synthesis](security-synthesis.md) (#953).
Load-path siblings: [NEFF Version / Compatibility Model](../../neff/version-compat.md)
(#878), [NEFF Container Byte Format](../../neff/container-byte-format.md),
[A Concrete NEFF, Carved](../../neff/concrete-carve.md) (#873/#880),
[The NEFF ↔ ELF Relationship](../../neff/neff-elf-relationship.md) (#832/#816),
[The Host Prelinker — UCPL](../../runtime/prelinker-ucpl.md),
[Opcode-Set → Library Resolver](../../runtime/opcode-to-lib-resolver.md)
(#827/#830), [nrtucode Subsystem + Device Bring-Up](../../runtime/nrtucode-bringup.md),
[SEQ Boot / Entry Path](../../firmware/seq/boot.md) (#653),
[NSM Fault → Isolation → IRQ](../interrupt/nsm-flow-unified.md) (#942).

> **PROVENANCE.** Host-runtime addresses are read from `libnrt.so.2.31.24.0`
> (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, sha256
> `956382de…02cd59a6`); device-library addresses are read this session straight
> from `libnrtucode_internal.so` (NOT stripped, BuildID `9cbf78c6…`) via
> `nm`/`objdump`. Confidence is tagged `[conf · prov]`, `prov ∈ {OBSERVED,
> INFERRED, CARRIED}`. The threat *consequences* (what an attacker could do given
> an observed gap) are reasoned `[MED · INFERRED]` from the observed control flow
> — the binary witnesses the *check*, the inference is the *attack it fails to
> stop*. **v5 / Maverick is header-OBSERVED only** (its EXTISA getters ship but
> its `DYNAMIC_KERNEL_LOAD` tables do not) → any statement about v5 *interior*
> load behaviour is `[v5-interior · INFERRED]` and flagged inline.

---

## 1. The integrity chain, end to end — what each step validates

A custom-op image crosses **eight** gates between the host filesystem and a
running Q7 core. The table is the map; §1.1–§1.8 are the byte-grounded detail.
Read the **"validates"** and **"does NOT validate"** columns as a pair — the
delta between them *is* the attack surface §2 enumerates.

| # | Step | Where (symbol @ addr) | Validates | Does **NOT** validate |
|---|------|-----------------------|-----------|-----------------------|
| 1 | **Host stages image** | `nrt_set_pool_eng_ucode @0xc1630` → `tdrv_set_pool_eng_ucode @0x2695f0` | only that `nrt_init_state==0` (pre-init) | image content, size, arch, origin |
| 2 | **Container integrity** | `neff_parse @0x4ca3f0` → `MD5_Update`/`sha256_*` | unkeyed digest over the **compressed** payload — *if* the verify flag is set | authenticity (unkeyed); skipped entirely when verify flag `==0` |
| 3 | **Version / compat gate** | `neff_parse` major ceiling; `kelf::load @0x497dc0`; `parse_one_ucode_lib @0x4b1610` | `major≤2`, feature-bit trapdoor, arch-skew, ucode **major** equality | minor versions; arch gate is env-overridable |
| 4 | **opcode-0x85 builtin / ExtISA bind** | `parse_one_ucode_lib @0x4b1610`; resolver `nrtucode_ll_get_libraries_from_opcodes @0x9b1880` | opcode `0x85` → built-in (flags 0) vs user ExtISA (flags 6); coretype in accepted set | opcode *content*; the lib's internal handler targets |
| 5 | **ELF magic / phdr shape** | `xtlib_verify_magic @0x9b6d40`; `validate_dynamic_load @0x9b71f0` | `\x7fELF`, `ELFCLASS32`, endian byte, `{R+X / R+W / DYNAMIC}` phdr shape | machine type, version, checksum, signature |
| 6 | **Prelink / relocate** | `prelink_relocate_lib @0x9b6160` → `relocate_op @0x9b6660` → `reloc_addr @0x9b6130` | `r_info ≤ 0xff`, per-segment size caps, 64 KiB total | reloc *targets* against any allow-list; self-relative only — **aborts** on any failure |
| 7 | **Stage into Q7 IRAM/DRAM** | `aws_hal_q7_ucode_eng_init @0x451080` → `write_padded @0x473eb0` → `al_mem_write_buf @0x265990` | per-segment region bounds (32 K code / 8 K data) | that the bytes are a *trusted* image — it just writes them |
| 8 | **Boot / claim handshake** | `nrtucode_core_on_ucode_booted @0x9b0ab0` | the 4-byte **liveness** sentinel `0x6099CB34`; single-owner claim | image authenticity — it is a readiness ping, not an auth |

> **WALL — the chain validates *integrity and compatibility*, never *authenticity*.**
> Every gate above answers "is this image well-formed and compatible?" Not one
> answers "is this image *authorized*?" The authorization boundary is **not in the
> image-load path at all** — it is the **host's exclusive control of the load
> path** plus the **silicon perimeter** (§3). A reimplementer who treats any of
> these eight gates as an authentication step has mis-modeled the trust chain.
> `[HIGH · OBSERVED]`

### 1.1 Step 1 — host stages the image (no content gate)

The registration entry `nrt_set_pool_eng_ucode @0xc1630` stores a raw
`{iram_bin, iram_size, dram_bin, dram_size}` quad into four runtime globals
(`pool_eng_iram_bin @0xc97030`, `…_size @0xc97028`, `pool_eng_dram_bin
@0xc97020`, `…_size @0xc97018`). The **only** precondition is `nrt_init_state==0`
(it must be called before `nrt_init`). No bytes are inspected. `[HIGH · OBSERVED;
CARRIED from nrtucode-bringup]`

The silent override that makes this a trust gap is in
`tpb_eng_init_hals_v2 @0x2687e0`: after loading the **shipped default** Q7 pool
image, **if** `pool_eng_iram_bin` is non-null it *unconditionally* overwrites the
HAL config's `(bin,size)` pairs (`hal_cfg[96]/[104]` IRAM, `hal_cfg[112]/[120]`
DRAM) with the caller's blob and `qmemcpy`s `0x450` bytes into the per-engine
struct — with **no size, signature, arch, or sanity check and no warning log**.

> **GOTCHA — the ucode override is unguarded and silent.** The runtime loads the
> signed/shipped default, then *replaces* it with the caller-registered blob with
> no validation and no diagnostic. A malformed image surfaces only later (the §1.7
> stage write or the §1.8 claim), never as a rejection here. This is the chain's
> **first and widest** host-trust dependency: whoever can call
> `nrt_set_pool_eng_ucode` before `nrt_init` controls the pool ucode outright.
> `[HIGH · OBSERVED; consequence MED · INFERRED]`

### 1.2 Step 2 — container integrity (unkeyed, optional)

The NEFF container check lives in `neff_parse @0x4ca3f0`. It dispatches on
`pkg_version` at header `+0x0`:

* **pkg1 (legacy):** raw GNU `ustar` tar + **SHA-256 (32 B, full `hash[]`)**,
  assert `neff.cpp:0x34`, mismatch → `"SHA256 mismatch!"` → `NRT_INVALID`.
* **pkg2 (current):** gzip-compressed GNU `ustar` tar + **MD5 (16 B, `hash[0..15]`)**,
  assert `neff.cpp:0x40`, mismatch → `"MD5 mismatch!"` → `NRT_INVALID`.

The digest is computed over the **compressed** `data[data_size]` window
(`header + 0x400`), **not** the inflated tar — proven by recomputation on the one
real NEFF in the corpus: `md5(1709-byte gzip) == hash[0..15] =
61b3cab2…a744c746`, while `md5(20480-byte inflated tar)` does **not** match.
`[HIGH · OBSERVED by recomputation; CARRIED #873/#880]`

```c
/* Step 2 — container integrity gate (reimplementation-grade).
 * Models neff_parse @0x4ca3f0. The digest is UNKEYED -> corruption only.
 * verify_flag is nrt_load's `a3`: when 0, NO digest is computed at all. */
typedef enum { NRT_OK = 0, NRT_INVALID = 2, NRT_UNSUPPORTED_NEFF = 10 } nrt_rc_t;

static nrt_rc_t neff_integrity_check(const neff_header_t *h,
                                     const uint8_t *data /* = (uint8_t*)h + 0x400 */,
                                     bool verify_flag)
{
    /* Structural identity FIRST -- there is NO magic number; identity is the
     * size invariants (container-byte-format.md): header_size==0x400, the
     * compressed payload must fit, major<=2. */
    if (h->header_size != 0x400)                 return NRT_INVALID;
    if (h->data_size  >  h->file_size - 0x400)   return NRT_INVALID;

    if (!verify_flag)
        return NRT_OK;            /* *** integrity is OPTIONAL at the loader seam ***
                                   * verify_flag==0 -> loader trusts the bytes blind */

    uint8_t digest[32];
    if (h->pkg_version == 1) {                   /* legacy: SHA-256 over compressed */
        sha256(data, h->data_size, digest);      /* sha256_* @0x5ca680.. */
        if (memcmp(digest, h->hash, 32) != 0)    return NRT_INVALID; /* "SHA256 mismatch!" */
    } else if (h->pkg_version == 2) {            /* current: MD5 over compressed gzip */
        md5(data, h->data_size, digest);         /* MD5_* @0x5ca0e0.. */
        /* QUIRK: compare EXACTLY hash[0..15]; hash[16..31] is ASCII '0' (0x30)
         * padding, NOT zero -- a full-32-byte compare would spuriously fail. */
        if (memcmp(digest, h->hash, 16) != 0)    return NRT_INVALID; /* "MD5 mismatch!"   */
    } else {
        return NRT_UNSUPPORTED_NEFF;             /* "Unsupported NEFF packager: %lu" */
    }
    return NRT_OK;
}
```

> **CORRECTION — the digest is integrity, and it is *optional*; both halves
> matter.** Two independent weaknesses compose here. (a) The hash is **unkeyed**:
> *"a modified NEFF with a recomputed hash passes"* — there is no signature or MAC,
> so it detects corruption, never tampering. (b) The check is **gated on
> `nrt_load`'s verify flag (`a3`)**: when that flag is clear the loader extracts
> the NEFF with **no digest comparison at all**. A threat model that assumes "the
> NEFF was hashed, so it is trustworthy" is wrong on *both* counts. `[HIGH ·
> OBSERVED]`

> **QUIRK — `hash[16..31]` is the ASCII string `"0000…"`, not zero padding.** The
> producer fills the 16 trailing digest bytes with ASCII `'0'` (`0x30`). A
> reimplementer must take **exactly `hash[0..15]`** for the pkg2 MD5 compare;
> zero-extending the 16-byte MD5 to 32 bytes and comparing the whole field would
> reject every valid pkg2 NEFF. `[HIGH · OBSERVED; CARRIED #880]`

### 1.3 Step 3 — version / compatibility gates (three tiers)

Three independent compatibility gates run, each refusing cleanly on mismatch.
Byte-detail is owned by [version-compat.md](../../neff/version-compat.md) (#878);
the security-relevant shape is:

```c
/* Step 3 — the three compat tiers. Each FAILS STOP (refuses), none authenticates. */
static nrt_rc_t neff_compat_gates(const neff_header_t *h, kelf_t *k)
{
    /* TIER 1 -- container major ceiling. neff_parse @0x4ca53c: cmp $0x2,%rax. */
    if (h->neff_version_major > 2)               /* accept {0,1,2} only            */
        return NRT_UNSUPPORTED_NEFF;             /* "...accepted version range: 0.x-2.x" */
    /* h->neff_version_minor (+0x20) is NOT range-checked -- informational only.   */

    /* TIER 1b -- feature-bit forward-compat trapdoor, armed ONLY for major==2.
     * If any unsupported bit is set, refuse. Mask @0x4ca790. NOT overridable.     */
    if (h->neff_version_major == 2 &&
        (h->feature_bits & 0x7FFFFFFFF8000000ull) != 0)  /* bits 27..62 reserved   */
        return NRT_UNSUPPORTED_NEFF;             /* supported = bits 0..26 (0x7FFFFFF) */

    /* TIER 2 -- arch skew. kelf::load @0x497dc0 compares the NEFF target ordinal
     * (parse_target @0x497c10, stored at kelf+0xB8) against the LIVE instance arch
     * al_hal_tpb_get_arch_type(). "*"->any, ""->SUNDA. */
    int want = k->target_arch;                   /* {SUNDA=2,CAYMAN=3,MARIANA=4} HAL scale */
    if (want != ARCH_ANY && want != al_hal_tpb_get_arch_type()) {
        /* *** OVERRIDABLE: NEURON_RT_ALLOW_LEGACY_NEFF=1 downgrades to a warning *** */
        if (!getenv_bool("NEURON_RT_ALLOW_LEGACY_NEFF"))
            return /* NEFF_ARCH_INCOMPAT */ NRT_INVALID;
    }

    /* TIER 3 -- GPSIMD ucode semver. parse_one_ucode_lib @0x4b1610 compares the
     * lib's "ulib_to_ucode_version" against the baked constant "1.21.1.0"
     * (.rodata 0x84fe68) -- MAJOR-EQUALITY ONLY (major==1); minor/patch ignored.
     * Missing -> "Missing GPSIMD lib version!". NOT overridable. */
    /* ...handled per-lib inside step 4... */
    return NRT_OK;
}
```

> **NOTE — `arch_id` → codename, and don't conflate the three numberings.** The
> hardware `arch_id` codename byte (`0x05`=SUNDA, `0x0c`=CAYMAN, `0x14`=MARIANA,
> `0x1c`=MARIANA_PLUS) is a *different* scale from the software/HAL
> `al_hal_tpb_arch_type` ordinal (`SUNDA=2`, `CAYMAN=3`, `MARIANA=4`) and from the
> ExtISA-lib-id triple `CSWTCH.113={6,13,21}`. The Tier-2 skew gate uses the HAL
> ordinal. `arch_id 0x0c ⇒ codename CAYMAN / NC-v3`. `[HIGH · OBSERVED; CARRIED
> #878]`

> **GOTCHA — exactly one env var unlocks exactly one tier.** `NEURON_RT_ALLOW_LEGACY_NEFF=1`
> relaxes **only** the Tier-2 arch-skew gate (and the legacy-statebuffer guard) to
> a warning. The Tier-1b feature-bit trapdoor and the Tier-3 ucode-major check
> have **no** override. A defender enumerating "what a config flag can loosen"
> needs only this one variable, and it never reaches integrity or authenticity —
> only arch compatibility. `[HIGH · OBSERVED]`

### 1.4 Step 4 — the opcode-`0x85` builtin / ExtISA bind

`parse_one_ucode_lib @0x4b1610` parses each NEFF `ucode_lib` section. The
discriminator that classifies a library is a **single opcode value**:

* `opcode == -123` (i.e. **`0x85`**) marks the **baked default** built-in library
  (`flags = 0`) — the runtime's own POOL ExtISA union, which lives in
  `libnrtucode_extisa.so`, **not** in the NEFF.
* any other opcode marks a **user-supplied ExtISA** library (`flags = 6`).

It also asserts `cpu_id == 0` (`kelf2kbin.cpp:0x691`) and `total_cpus ∈ {0,1,8}`.
`[HIGH · OBSERVED; CARRIED #878/#827/#830]`

A second, host-side resolver picks *which* library backs a model's opcode set:
`nrtucode_ll_get_libraries_from_opcodes @0x9b1880` (internal). Its security-relevant
property is that it is **opcode-content-blind**:

> **CORRECTION — the opcode→library resolver is a *picker*, not a *validator*.** It
> *never reads the opcode array*. It keys solely on `(core_type, num_opcodes != 0,
> NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE env)` and emits a single scalar index
> `{0 = EXTISA_0 base \| 3 = EXTISA_3 CPTC superset}`. The only bound it enforces
> is a **coretype** gate (`cmp $0x25`; bitmask `0x2020202000` ⇒ accepted Q7-POOL
> set `{6,13,21,29}`; everything else → `NRTUCODE_ERR_UNKNOWN_CORE`). It performs
> **no per-opcode lookup, no dependency closure, and no verification that the
> chosen library actually implements the requested opcodes.** A reimplementer must
> implement a *picker*, not a union/OR over a bitmap. `[HIGH · OBSERVED]`

The actual opcode→handler binding happens **on-device**, one stage later: the SEQ
hands an `EXTENDED_INST (0xF0)` to a Q7 core, and the device runtime linear-scans
the in-DRAM `kernel_info_table` (device VAddr `0x02000380`, rows
`{0,0,spec,opcode,funcVA}`) for the `(spec, 0xF0)` row and `callx8`s its `funcVA`.
The observed table is internally consistent (every `funcVA` lands inside the
IRAM text segment `[0x01000000, 0x01006F1E)`) — but that is an **observed
property of the shipped image, not an enforced runtime bounds check** on the
target. `[HIGH · OBSERVED layout; MED · INFERRED that no runtime target-bound is
applied]`

### 1.5 Step 5 — ELF magic and program-header shape

When the staged image is itself an Xtensa `pi_library` ELF (the custom-op ExtISA
`.so`), two structural gates run before relocation. Re-disassembled this session
from `libnrtucode_internal.so`:

```c
/* Step 5a -- xtlib_verify_magic @0x9b6d40 (re-disassembled this session).
 * Validates ONLY: ELF magic, 32-bit class, and the endian byte (which it uses to
 * set a global byte-swap flag). It does NOT check e_machine, e_version, or ANY
 * integrity field. Returns 0 on success (xor %eax,%eax). */
static int xtlib_verify_magic(const uint8_t *e /* e_ident */)
{
    if (e[0]!=0x7f || e[1]!='E' || e[2]!='L' || e[3]!='F') return -1; /* \x7fELF */
    if (e[4] != 1) return -1;                       /* EI_CLASS == ELFCLASS32 (required) */
    int data = e[5];                                /* EI_DATA: 1=LE, 2=BE              */
    if (data != 1 && data != 2) return -1;
    xtlib_globals.byteswap = (data == 2);           /* the ONLY "version-like" use of e_ident */
    return 0;                                        /* magic OK -- nothing else checked */
}

/* Step 5b -- validate_dynamic_load @0x9b71f0 (re-disassembled this session).
 * Walks the phdr table and enforces the segment SHAPE: a R+X text LOAD, a R+W
 * data LOAD, an optional R+W scratch LOAD, and a PT_DYNAMIC (flags&7==6).
 * On any shape failure it writes error code 7 -- it does NOT abort the process,
 * it returns a structured error to the caller. */
static int validate_dynamic_load(xtlib_info_t *out, const elf32_ehdr_t *e)
{
    memset(out, 0, 0x30);
    if (xtlib_verify_magic((const uint8_t*)e) != 0) { out->rc = 7; return 7; }
    /* phdr[0] must be PT_LOAD (type==1) with (p_flags & 7)==5  (R + X)  -> text  */
    /* next  must be PT_LOAD with (p_flags & 7)==6  (R + W)             -> data  */
    /* optional PT_LOAD with (p_flags & 6)==2                          -> scratch*/
    /* final  PT_DYNAMIC with (p_flags & 7)==6                                   */
    /* ...each checked via xtlib_host_word (the endian-aware word reader)...      */
    /* mismatch anywhere: out->rc = 7  (NOT a security trap)                      */
    return out->rc;
}
```

> **NOTE — `validate_dynamic_load` validates *shape*, not *machine* and not
> *trust*.** It enforces the `{R+X, R+W, [R+W], DYNAMIC}` four-phdr contract and
> nothing else. It does not check `e_machine` (94 = Xtensa), `e_version`, a
> checksum, or a signature. The masks are *looser* than canonical (they ignore the
> X bit on the data segment). A malformed-but-shape-correct ELF passes structural
> validation; its bytes are caught — if at all — only by the relocation walk
> (§1.6) and the device perimeter (§3), never here. `[HIGH · OBSERVED]`

### 1.6 Step 6 — prelink / relocate (self-relative, abort-on-fail)

The host prelinker rebases the `pi_library` to its device load addresses by
walking the `.rela.got` table (240 `Elf32_Rela` entries on the reference blob).
The chain is `prelink_relocate_lib @0x9b6160 → relocate_op @0x9b6660 →
reloc_addr @0x9b6130`. Re-disassembled this session, the dispatch keys on the
`r_info` low byte:

```c
/* Step 6 -- prelink_relocate_lib @0x9b6160 (re-disassembled this session).
 * Type 0 = R_XTENSA_NONE (skip); type 5 = additive 32-bit rebase (see #881 note);
 * 20..34 = R_XTENSA_SLOT{0..14}_OP; 35..49 = R_XTENSA_SLOT{0..14}_ALT.
 * Any relocate_op() failure ABORTS the whole install -- the image is NEVER
 * partially relocated and staged. There is NO dynamic-symbol lookup: a packaged
 * pi_library must be fully self-relative. */
static int prelink_relocate_lib(prelink_ctx_t *ctx, xtlib_info_t *lib)
{
    for (long i = 0; i < lib->n_relocs; i++) {
        elf32_rela_t r = lib->relocs[i];
        uint8_t type = r.r_info & 0xff;
        if ((r.r_info) > 0xff)                 /* symbol index present -> reject */
            return /* rc 5 */ XTLIB_UNKNOWN_SYMBOL;   /* "degenerate by contract" */

        uint32_t at = reloc_addr(lib, r.r_offset);     /* segment-base arithmetic only */
        int rc = relocate_op(lib, at, type, r.r_addend /* + rebase delta */);
        if (rc != 0)                            /* relocate_op rc 8 on bad slot */
            return rc;                          /* *** ABORT: fail stop ***      */
    }
    return 0;
}
```

> **CORRECTION — type 5 is `R_XTENSA_RELATIVE` (value 5), with the #881 mnemonic
> fix; the prelinker page dissents on the *name*.** The relocation *value* is 5 and
> the *semantics* are an additive 32-bit rebase — agreed everywhere. The committed
> consumer page [neff-elf-relationship.md](../../neff/neff-elf-relationship.md)
> names it **`R_XTENSA_RELATIVE`** (the official `ncore2gp` Tensilica `readelf`
> mnemonic, grounded in the 30×-of-240 histogram — the **#832/#816 + #881** fix);
> [prelinker-ucpl.md](../../runtime/prelinker-ucpl.md) standardizes on
> `R_XTENSA_32`. This page follows the consumer page and the #881 fix:
> **`R_XTENSA_RELATIVE`, type 5**. The split is a *label* disagreement only; the
> security-relevant facts (value 5, additive rebase, **self-relative, abort on
> failure**) are unanimous. `[HIGH · OBSERVED value/semantics; mnemonic per #881]`

> **GOTCHA — relocation is the strongest structural gate, and it is purely
> internal-consistency.** Unlike the integrity hash (optional) and `verify_magic`
> (magic only), relocation **always runs and aborts on any failure** — so a
> structurally broken image cannot reach a core half-relocated. But it validates
> the image against **itself** (segment-base arithmetic, no symbol table, no
> external allow-list). It cannot tell a *legitimate* self-consistent image from a
> *malicious* one. It is a robustness gate, not a trust gate. `[HIGH · OBSERVED;
> consequence MED · INFERRED]`

### 1.7 Step 7 — stage the bytes into Q7 IRAM/DRAM

`aws_hal_q7_ucode_eng_init @0x451080` → `write_padded @0x473eb0` copies the
prelinked IRAM/DRAM segments into the eight pool cores via `al_mem_write_buf
@0x265990` (the memhandle/MMIO write path; the device-side staging uses the
memhandle vtable with `device_malloc` of a 16 MiB buffer and three
`write_memhandle` calls — header, code, data). Per-segment region bounds are
enforced (`cayman_memory_bounds @0x9aaee0`: code base `0x18000` size `0x8000`/32 K,
data base `0xbe000` size `0x2000`/8 K) plus a **64 KiB** total-image ceiling
(`cmpq $0x10001`), over which it returns `"Prelinked library would be larger than
the available buffer on device"` (rc 7). `[HIGH · OBSERVED; CARRIED prelinker-ucpl]`

> **CORRECTION — the device-side stage is a *memhandle / MMIO* write, not "BAR0
> DMA."** The only BAR-numbered path observed anywhere on the load path is the
> `ndl_memory_buf_zerocopyin` **BAR4** zero-copy weight path (a *data* path, not
> the *ucode* path). The ucode segment write is `write_padded @0x473eb0` →
> `al_mem_write_buf @0x265990` through the platform memhandle vtable — a
> broadcast PIO/DMA to all eight Q7 pool cores. A threat model that says "BAR0 DMA
> stages the ucode" would not agree with the binary; the correct statement is
> "the host stages the bytes via the platform memhandle MMIO path, bounded by the
> per-segment region size and the 64 KiB total cap." `[HIGH · OBSERVED by absence
> of any BAR0 reference on this path]`

> **NOTE — bounds at this step protect the *device layout*, not the *tenant*.** The
> 32 K/8 K/64 KiB caps stop an oversized image from clobbering adjacent device
> regions on the core it targets. They do **not** decide *whether* this host should
> be staging *this* image to *these* cores — that decision was already delegated to
> the host at Step 1. Cross-core / cross-tenant containment is the **silicon
> perimeter's** job, not this write's (§3). `[HIGH · OBSERVED bound; MED · INFERRED
> scope]`

### 1.8 Step 8 — the boot / claim handshake (liveness, not auth)

`nrtucode_core_on_ucode_booted @0x9b0ab0` is the host's final step. Re-disassembled
this session, it is a **4-byte read + a 4-byte write** through the platform
read/write vtable — it boots nothing and copies nothing:

```c
/* Step 8 -- nrtucode_core_on_ucode_booted @0x9b0ab0 (re-disassembled this session).
 * A liveness + single-owner handshake over DRAM[0]. NOT an authentication. */
static int nrtucode_core_on_ucode_booted(nrtucode_core_t *core)
{
    uint32_t w;
    if (core->platform_read(core->ctx, /*off*/0, /*len*/4, &w) != 0)   /* read DRAM[0] */
        return -1;                                                      /* I/O error    */

    if (w == 0x6099CB34) {                 /* *** the device-written READY sentinel *** */
        core->platform_write(core->ctx, 0, 4, /*value*/ 0x502B2DA1);    /* host CLAIM   */
        core->boot_state = 1;              /* BOOTED                                    */
        return 0;
    }
    if (w == 0x502B2DA1) {                 /* already our claim word -> double-claim    */
        nrtucode_context_log(/*..."Core is claimed by another nrtucode_core_t..."*/);
        return 8;                          /* NRTUCODE error -- single-owner guard      */
    }
    return /* not-yet-booted */ 0;         /* sentinel absent: core not ready yet       */
}
```

The device firmware writes `0x6099CB34` to `DRAM[0]` once its POOL ucode is up
(re-verified on the carved DRAM blob as LE `34 cb 99 60` at offset 0 by the
[boot-arming page](boot-arming-fault-recovery.md) §5c). The host reads it,
**writes back `0x502B2DA1`** as its ownership claim, and marks the core booted.
A second `nrtucode_core_t` that finds `0x502B2DA1` already present is refused
(`"Core is claimed by another…"`, rc 8). `[HIGH · OBSERVED]`

> **NOTE — the claim handshake authenticates *nothing*.** `0x6099CB34` proves the
> device firmware *reached its run loop*; `0x502B2DA1` records *which host owns the
> core*. Neither value depends on, or attests to, the *content* of the staged
> image — a host that staged a malicious image gets exactly the same `0x6099CB34`
> back. The handshake is a liveness + mutual-exclusion primitive, and a defender
> must not read it as proof the running ucode is the intended one. `[HIGH ·
> OBSERVED; consequence MED · INFERRED]`

---

## 2. Threat model — what a malicious NEFF or custom op could attempt

Given §1, an adversary who can submit a NEFF or register a custom op (the
**guest / untrusted-tenant** position) has the following candidate moves. For
each: the **attempt**, what **stops** it, and what it **relies on the host
being trusted** for.

| # | Adversary attempt | What STOPS it | What it RELIES ON (host trusted) |
|---|-------------------|---------------|----------------------------------|
| T1 | **Tampered ucode** (modified image) | nothing in the image path — hash is unkeyed & optional (§1.2); override is unchecked (§1.1) | the host never loading an untrusted image; host-exclusive `nrt_set_pool_eng_ucode` |
| T2 | **Out-of-bounds DMA** from a running op (read/write outside its buffers) | **silicon perimeter**: `user_remapper` CAM + the `amzn` firewall around it; NTS terminator; NSM watchdog (§3) | the perimeter being **armed** by privileged firmware (boot-arming #948) |
| T3 | **Privilege escalation toward "Pacific"** (guest tries to act privileged) | **plane + AxPROT split**: guest plane cannot address the secure CSRs; guest master physically cannot emit `AxPROT 0x2`; CSR-bus deny-all (§3) | the immutable reset split (`pass_on_miss`, `master_prot` present only on `amzn`) |
| T4 | **Cross-core / cross-tenant interference** | **on-core MPU** + the perimeter remapper region/ID gating + reachability cuts (§3, #950) | MPU programmed at boot; remapper CAM provisioned per tenant |
| T5 | **Silent ucode override** (replace pool image) | nothing — §1.1 is unguarded | host-exclusive registration before `nrt_init`; the host not exposing it to a guest |
| T6 | **Malformed image to crash a core** | relocation abort (§1.6); fail-stop fault machine (#948 §5) | — (this is *contained*: it self-halts and reports, never silently continues) |
| T7 | **Version/arch downgrade** to a weaker image | Tier-1/2/3 compat gates (§1.3) refuse | the `NEURON_RT_ALLOW_LEGACY_NEFF` env var not being set by the host for a guest |

> **WALL — the integrity chain stops *accidents*; the *silicon perimeter* stops
> *attacks*; the *host* stops *unauthorized images*.** Map the three rows of
> defense: (1) the §1 gates are **robustness** — they catch corruption, version
> skew, and malformed structure, and they fail stop. (2) The §3 perimeter is
> **containment** — it bounds what a *running* image can touch on the bus,
> regardless of how it got there. (3) The **host** is the **authorization** root —
> nothing in hardware or device firmware decides whether an image is *allowed*,
> only whether it is *well-formed* (chain) and *well-behaved at runtime*
> (perimeter). Remove the host's trust and the only remaining defense is
> containment, not authenticity. `[HIGH · OBSERVED structure; MED · INFERRED
> threat mapping]`

### 2.1 The host-trust dependency, made explicit

Three observed facts make the host the authorization root:

1. **The image path never authenticates** (§1, T1/T5). Authorization is *absence
   of a check*, delegated upward.
2. **The host alone reaches the load path.** `nrt_set_pool_eng_ucode` is a
   pre-`nrt_init` host API; a guest NEFF cannot call it. The perimeter's CSR-bus
   policy (`block_host_access.yaml`: `_sprot_allow:False` **except**
   `apb.misc_ram`) means the host's CSR protocol reaches **only MISC_RAM** — a
   guest reaches even less. `[HIGH · OBSERVED; CARRIED #948 §3]`
3. **The host alone arms the perimeter** (boot-arming #948 §4). A compatible host
   runtime **cannot arm the fabric itself**; it relies on the privileged
   management firmware having already armed it. If the perimeter is *not* armed,
   T2–T4 are unstopped — the device is "born permissive."

> **GOTCHA — "armed, not default-secure" is the whole posture in four words.** Out
> of reset, four of the five perimeter blocks are open/off and exactly one
> (`amzn_remapper`) boots fail-closed (#948 §2). The trust chain's containment is
> *latent* until privileged firmware runs the arming sequence. A defender modeling
> the device before arming must assume **T2–T4 are open**; after arming, they are
> closed by the perimeter. `[HIGH · OBSERVED; CARRIED #948]`

---

## 3. The trust boundary — what stops what

```text
  ============================ TRUST BOUNDARIES (Cayman / Vision-Q7) ============================

  [ UNTRUSTED ]                    | [ TRUSTED HOST ]              | [ DEVICE: silicon + fw ]
   NEFF author / custom-op /       |  Neuron runtime (libnrt)      |
   PCIe guest master               |  privileged mgmt fw ("Pacific")|
  ---------------------------------+-------------------------------+------------------------------
                                   |                               |
   submits NEFF ------------------>| STEP 1 stage  (no content gate)|
                                   | STEP 2 integrity (unkeyed/opt) |
                                   | STEP 3 compat   (3 tiers, stop) |
                                   | STEP 4 opcode-0x85 bind         |
                                   | STEP 5 magic/phdr shape         |
                                   | STEP 6 prelink/reloc (abort)    |
                                   | STEP 7 stage to Q7 -----------> | IRAM/DRAM bytes land
                                   | STEP 8 claim handshake <------> | DRAM[0] 6099CB34 -> 502B2DA1
                                   |                               |
   ===== AUTHORIZATION ROOT = the host (no key, no signature) =====|
                                   |                               |
   running op issues AXI txn ======|===============================|=====> SoC-FABRIC PERIMETER (#947):
   {addr[57:0], id[9:0], rd/wr}    |    (guest plane only)         |   WATCH  nsm  (9 protocol shapes)
                                   |                               |   DECIDE remapper CAM:
                                   |                               |     user(guest): fail-OPEN 0x1
                                   |                               |     amzn(priv) : fail-CLOSED 0x0
                                   |                               |     AxPROT 0x2 = amzn ONLY
                                   |                               |   RESPOND nts  SLVERR+0xDEADBEEF
                                   |                               |
                                   |                               +-> on-core MPU (16 regions, boot.md)
                                   |                               +-> reachability cuts (#950)
                                   |                               +-> PSUM / per-core boundaries
                                   |                               |
   ----- the guest is CONTAINED by the perimeter it cannot reach, NOT by its own default-deny -----
```

What the **silicon perimeter actually STOPS** (owned by
[soc-fabric-perimeter.md](soc-fabric-perimeter.md) #947 — byte-detail there):

* **Out-of-bounds / forbidden AXI** (T2): the `user_remapper` CAM gates
  `{address region, 10-bit AXI master-ID}` and, on a denied or absent target, the
  **NTS** responder terminates locally with `SLVERR/DECERR + 0xDEADBEEF` so the
  fabric never hangs. The guest CAM boots **fail-OPEN** (`pass_on_miss=0x1`); it
  is contained by the **surrounding privileged `amzn` firewall** (fail-CLOSED
  `0x0`, which can wipe and bypass-ID the guest CAM) and the NTS error path, **not
  by its own default-deny**.
* **Privilege escalation toward "Pacific"** (T3): the guest plane cannot address
  the secure PEB_APB_IO CSRs (the fail-closed `amzn_remapper` gates them), and a
  guest master **physically has no `master_prot` register** — it can never set or
  override the `AxPROT 0x2` privileged sideband. The CSR-bus policy keeps the host
  out of the arming registers entirely; a guest reaches even less.
* **AXI protocol abuse** (T2/T6): the **NSM** watchdog inline on a PCIe master's
  path latches nine malformed shapes (stall / timeout / spurious / RLAST),
  injects an error response + 256-bit poison, and feeds the PCIe isolation SM —
  see [nsm-flow-unified.md](../interrupt/nsm-flow-unified.md) (#942).

What **STOPS cross-core / cross-tenant** (T4):

* The **on-core MPU** (`_ResetHandler @0x90` programs a 16-region MPU; #653
  boot.md §3) bounds each core's own reach; **PSUM-unreachable** and the
  per-engine **reachability cuts** are owned by
  [reachability-isolation.md](reachability-isolation.md) (#950). `[CARRIED #653/#950]`

What **RELIES ON THE HOST** (no silicon enforces it):

* **Image authenticity** (T1/T5) — *no* hardware/firmware check; the host must not
  load an untrusted image (§2.1).
* **Perimeter arming** (T2–T4 latent) — the host's privileged firmware must run
  the arming sequence; a compatible runtime cannot arm the fabric itself
  (#948 §3 CORRECTION: there is **no global perimeter-lock bit** — the "lock" is
  the trust boundary, not a latch).
* **Per-tenant CAM provisioning** — the perimeter *enforces* region/ID rules, but
  the host *programs* them; an unprovisioned guest is contained only by the
  surrounding `amzn` firewall, not by a CAM it hasn't been given.

> **NOTE — the compute Q7 is unprivileged; "Pacific" is privileged — the split is
> the escalation boundary.** The eight compute pool Q7 cores run the
> custom-op/NEFF image as an **unprivileged** master; the management "Pacific" Q7
> is the **privileged** core that arms the perimeter, owns the secure CSR plane,
> and receives the critical fabric-fault IRQ (`intr_peb_nsm_axi_timeout`,
> apex idx 111, `critical:1`). A guest running on a compute Q7 that tries to reach
> "Pacific" hits the plane gate and the AxPROT-stamp asymmetry — it can never
> claim privileged attributes on the bus. `[HIGH · OBSERVED; CARRIED #942/#947]`

---

## 4. Adversarial self-verification

The five strongest **validated-vs-not** claims, re-checked this session against
the named files (single-file `nm`/`objdump`/recomputation, never a folder grep):

| # | Claim | Check against | Result |
|---|-------|---------------|--------|
| 1 | **No cryptographic root of trust** — zero signature/MAC primitives on the GPSIMD load path | `nm libnrtucode_internal.so \| rg -i 'rsa\|ecdsa\|ed25519\|aes\|hmac\|verify_sig\|pubkey'`; libnrt has **no** libcrypto `DT_NEEDED` | **0 hits** in the device lib; runtime crypto-free ✓ |
| 2 | **Integrity is unkeyed AND optional** — MD5 over compressed gzip, skipped when verify flag `==0` | `neff_parse @0x4ca3f0` (`MD5_Update(data, data_size)`); recomputation `md5(1709-B gzip)==hash[0..15]` | digest over **compressed** payload; gated on `a3` ✓ (CARRIED #880) |
| 3 | **`xtlib_verify_magic` checks magic + class + endian only** — no machine/version/checksum | `objdump -d 0x9b6d40..0x9b6d90` | `cmpb 7F/45/4C/46`, `cmpb 0x4(rdi),1` (CLASS32), `EI_DATA∈{1,2}`, `xor %eax,%eax` ✓ |
| 4 | **Relocation aborts on failure, no symbol lookup** — robustness gate, self-relative only | `objdump -d 0x9b6160..` (`cmp $0xff` → UNKNOWN_SYMBOL; `jne` to abort on `relocate_op` rc≠0) | `cmp $0xff,%edx; ja` reject; `relocate_op` rc≠0 → `jne 9b662e` abort ✓ |
| 5 | **Boot/claim is a 4-byte liveness ping, not auth** — `0x6099CB34` → `0x502B2DA1`, content-blind | `objdump -d 0x9b0ab0..` (`cmp $0x6099cb34`; `movl $0x502b2da1`); claim-log string @ `.rodata 0x54e0` | read 4 → cmp sentinel → write claim; `"Core is claimed by another…"` ✓ |

The `nrt_set_pool_eng_ucode` unchecked-override (§1.1), the three compat tiers
and the single env override (§1.3), the opcode-`0x85` builtin/ExtISA flags split
(§1.4), the `validate_dynamic_load` shape-only contract (§1.5), and the
memhandle-MMIO (not BAR0) stage path (§1.7 CORRECTION) were re-confirmed against
their source files. **No claim failed.** The strongest correction surfaced is the
**BAR0 → memhandle/MMIO** stage-path fix (§1.7): the only BAR-numbered path on the
load is the **BAR4** zero-copy *weight* path, not a "BAR0 DMA" of the ucode.

---

## 5. Reimplementation / defender checklist `[summary]`

* **Root of trust is the host, not a key.** No signature, no MAC, no attestation
  anywhere on the GPSIMD load path. Treat the host as the authorization boundary;
  a reimplementer adding "the device verifies the image" has invented a check that
  does not exist.
* **Integrity = MD5/SHA-256 over the *compressed* payload, unkeyed, and *optional*
  behind `nrt_load`'s verify flag.** Compare exactly `hash[0..15]` for pkg2 (the
  tail is ASCII `'0'`). It detects corruption, never tampering.
* **Three compat tiers fail stop:** container `major≤2` + feature-bit trapdoor
  (no override), arch skew (`NEURON_RT_ALLOW_LEGACY_NEFF` is the *only* loosener),
  ucode **major-equality** vs `"1.21.1.0"` (no override).
* **opcode `0x85` (=-123) = baked built-in (flags 0); else user ExtISA (flags 6).**
  The host resolver is a coretype/env *picker*, not an opcode validator; the
  device `kernel_info_table` does the `(spec,0xF0)` handler bind.
* **`verify_magic` = magic/class/endian only; `validate_dynamic_load` = phdr
  shape only; relocation = self-relative, abort-on-fail.** None of the three is a
  trust gate; only relocation is non-optional, and it validates the image against
  itself.
* **Stage is a memhandle/MMIO write (NOT BAR0 DMA), bounded by 32 K code / 8 K
  data / 64 KiB total.** Boot/claim is a liveness + single-owner handshake
  (`0x6099CB34` → `0x502B2DA1`), not authentication.
* **Containment is the perimeter, arming is the host.** A *running* malicious op
  is bounded by the armed remapper/NSM/NTS perimeter (#947), the on-core MPU
  (#653), and the reachability cuts (#950) — but those rely on the host's
  privileged firmware having **armed** the perimeter (#948); the device is born
  permissive (`armed, not default-secure`), fails stop, and has **no global lock
  bit**.
