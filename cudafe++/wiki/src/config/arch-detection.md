# Architecture Detection

cudafe++ determines the target GPU architecture through a five-stage pipeline: nvcc translates the user-facing `--gpu-architecture=sm_XX` flag into an internal numeric index, passes it to cudafe++ via `--target`, the CLI parser stores the index in a global, `set_target_configuration` configures over 100 type-system globals for that target, and the TU initializer copies the index into per-translation-unit state where it is read by feature gates throughout compilation. A parallel path, `select_cp_gen_be_target_dialect`, routes the backend to emit either device-side or host-side C++ based on a separate flag. This page documents the complete chain from nvcc invocation to the point where individual feature checks read the stored architecture value.

## Key Facts

| Property | Value |
|---|---|
| Target index global | `dword_126E4A8` (set by `--target`, CLI case 245) |
| Invalid sentinel | `-1` (`0xFFFFFFFF`) |
| Error on invalid target | Error 2664: `"invalid or no value specified with --nv_arch flag"` |
| Target parser stub | `sub_7525E0` (6 bytes, returns `-1` unconditionally) |
| Configuration function | `sub_7525F0` (`set_target_configuration`, `target.c:299`) |
| Type table initializer | `sub_7515D0` (100+ globals, called from `sub_7525F0`) |
| Configuration validator | `sub_7527B0` (`check_target_configuration`, `target.c:512-659`) |
| Field alignment initializer | `sub_752DF0` (`init_field_alignment_tables`, `target.c:825`) |
| Dialect selector | `sub_752A80` (`select_cp_gen_be_target_dialect`, `target.c:736`) |
| TU-level copy | `dword_126EBF8` (`target_configuration_index`, set in `sub_586240`) |
| GPU mode flag | `dword_126EFA8` (set by `--gcc`, case 182; gates dialect selection) |
| Device-side flag | `dword_126EFA4` (set by `--clang`, case 187; selects device vs host output) |

## The Full Propagation Chain

The architecture value flows through five distinct stages before it is available for feature gate checks. Each stage adds a layer of processing: parsing, validation, type model configuration, dialect routing, and per-TU state replication.

```text
Stage 1: nvcc                         Stage 2: CLI parsing
  --gpu-architecture=sm_90    --->      case 245 (--target)
  translates to --target=<idx>          sub_7525E0(<arg>) -> dword_126E4A8
                                        if -1: error 2664, abort
                                            |
                                            v
Stage 3: Target init                   Stage 4: Dialect selection
  sub_7525F0(idx)                        sub_752A80()
    assert idx != -1                       if dword_126EFA8 (GPU mode):
    sub_7515D0()   -> 100+ type globals      if dword_126EFA4: device path
    qword_126E1B0 = "lib"                    else: host path
    sub_752DF0()   -> alignment tables
    sub_7527B0()   -> validation
                                            |
                                            v
Stage 5: TU initialization
  sub_586240()
    dword_126EBF8 = dword_126E4A8  (per-TU copy)
    version marker: "6.6\0"
    timestamp copy
                                            |
                                            v
Feature checks throughout compilation
  if (dword_126E4A8 < 70) { error("__grid_constant__ requires compute_70"); }
  if (dword_126E4A8 < 80) { error("__nv_register_params__ requires compute_80"); }
  ...
```

## Stage 1: nvcc Translates the Architecture

Users specify the GPU architecture through nvcc:

```bash
nvcc --gpu-architecture=sm_90 source.cu
```

nvcc translates this into an internal numeric index and passes it to cudafe++ as `--target=<index>`. The value stored in `dword_126E4A8` is NOT a raw SM number like 90 -- it is an index into EDG's target configuration table. nvcc performs the mapping from user-facing strings (`sm_90`, `compute_80`, etc.) to this index. cudafe++ never sees the `sm_XX` string directly.

The `--target` flag is registered as CLI flag 253 with the internal `case_id` 245 in the flag table:

```c
// From sub_452010 (init_command_line_flags)
sub_451F80(245, "target", 0, 1, 1, 1);
//         ^id   ^name   ^no_short ^has_arg ^mode ^enabled
```

## Stage 2: CLI Parsing (proc_command_line, case 245)

When `proc_command_line` (`sub_459630`) encounters `--target`, it dispatches to case 245:

```c
// sub_459630, case 245 (decompiled)
case 245:
    v80 = sub_7525E0(qword_E7FF28, v23, v20, v30);
    dword_126E4A8 = v80;                   // store target index
    if (v80 == -1) {
        sub_4F8420(2664);                   // emit error 2664
        // "invalid or no value specified with --nv_arch flag"
        sub_4F2930("cmd_line.c", 12219,
                   "proc_command_line", 0, 0);  // assert-fail
    }
    sub_7525F0(v80);                        // set_target_configuration
    goto LABEL_136;                         // continue parsing
```

The error string references `--nv_arch`, which is the nvcc-facing name for this flag. Internally cudafe++ processes it as `--target` (case 245). The discrepancy exists because the error message is shared with nvcc's error reporting path.

### The sub_7525E0 Stub

`sub_7525E0` is the architecture parser function. In the CUDA Toolkit 13.0 binary, it is a 6-byte stub:

```c
// sub_7525E0 -- 0x7525E0, 6 bytes
__int64 sub_7525E0()
{
    return 0xFFFFFFFFLL;  // always returns -1
}
```

```asm
; IDA disassembly
sub_7525E0:
    mov     eax, 0FFFFFFFFh
    retn
```

This stub always returns the invalid sentinel `-1`. The actual architecture code reaches `dword_126E4A8` through the argument value passed by nvcc, not through parsing logic within this function. The function signature in the call site (`sub_7525E0(qword_E7FF28, v23, v20, v30)`) shows that four arguments are passed, but the stub ignores all of them. This means either:

1. The actual parsing is performed by nvcc, which passes the pre-resolved numeric index as the argument string, and `sub_7525E0` simply converts it with `strtol` -- but the link-time optimization eliminated the body because the result was equivalent to the argument itself.

2. The function is a placeholder that was replaced at link time by a different object file that nvcc provides when building the toolchain.

In either case, the return value `-1` is only reached when no valid `--target` argument is provided, which triggers error 2664.

## Stage 3: set_target_configuration (sub_7525F0)

After the target index is stored, `sub_7525F0` performs the post-parse initialization. This function lives in `target.c:299`:

```c
// sub_7525F0 -- set_target_configuration
__int64 __fastcall sub_7525F0(int a1)
{
    // Guard: accepts any value >= 0, rejects only -1
    // (a1 + 1) wraps -1 to 0, and (0u > 1) is false
    // Any non-negative value + 1 > 1 would be true... BUT this is unsigned:
    // -1 + 1 = 0, 0 > 1u = false (passes)
    // 0 + 1 = 1, 1 > 1u = false (passes)
    // The guard actually fires when a1 <= -2 (e.g., -2 + 1 = -1, cast unsigned = huge)
    if ((unsigned int)(a1 + 1) > 1)
        assert_fail("target.c", 299, "set_target_configuration", 0, 0);

    sub_7515D0();              // initialize type tables
    qword_126E1B0 = "lib";    // library search path prefix
    return -1;                 // return value unused
}
```

The unsigned comparison `(a1 + 1) > 1u` accepts values 0 and -1, rejecting everything else. In practice, only 0 or a valid non-negative target index reaches this function (the `-1` case is caught earlier by the error 2664 check). The guard is a sanity assertion rather than a functional check.

### Type Table Initialization (sub_7515D0)

`sub_7515D0` is the core of Stage 3. It sets over 100 global variables that define the target platform's data model. These globals control how the EDG front end sizes types, computes alignments, and evaluates constant expressions. The function hardcodes an LP64 data model with CUDA-specific properties:

```c
// sub_7515D0 -- target type initialization (complete decompilation)
__int64 sub_7515D0()
{
    // === Integer type sizes (in bytes) ===
    dword_126E338 = 4;     // sizeof(int)
    dword_126E328 = 8;     // sizeof(long)
    dword_126E410 = 4;     // sizeof(short) [confirmed by cross-ref]
    dword_126E420 = 2;     // sizeof(wchar_t)

    // === Pointer properties ===
    dword_126E2B8 = 8;     // sizeof(pointer)
    dword_126E2AC = 8;     // alignof(pointer)
    dword_126E4A0 = 8;     // target bits-per-byte (CHAR_BIT)
    dword_126E29C = 8;     // sizeof(ptrdiff_t)

    // === Floating-point properties ===
    // float: 24-bit mantissa, exponent range [-125, 128]
    dword_126E264 = 24;    // float mantissa bits
    dword_126E25C = 128;   // float max exponent
    dword_126E260 = -125;  // float min exponent

    // double: 53-bit mantissa, exponent range [-1021, 1024]
    dword_126E258 = 53;    // double mantissa bits
    dword_126E250 = 1024;  // double max exponent
    dword_126E254 = -1021; // double min exponent

    // long double: 16 bytes, same as __float128
    dword_126E2FC = 16;    // sizeof(long double)
    dword_126E308 = 16;    // alignof(long double)

    // __float128: 113-bit mantissa, exponent range [-16381, 16384]
    dword_126E234 = 113;   // __float128 mantissa bits
    dword_126E22C = 0x4000; // __float128 max exponent (16384)
    dword_126E230 = -16381; // __float128 min exponent

    // 80-bit extended (x87): same parameters as __float128
    dword_126E240 = 64;    // x87 extended mantissa bits
    dword_126E238 = 0x4000; // x87 extended max exponent
    dword_126E23C = -16381; // x87 extended min exponent
    dword_126E24C = 64;    // another extended format (IBM double-double?)
    dword_126E244 = 0x4000;
    dword_126E248 = -16381;

    // === Alignment properties ===
    dword_126E400 = 8;     // alignof(long long)
    dword_126E3F0 = 8;     // alignof(double)
    dword_126E35C = 8;     // alignof(long)
    dword_126E3E0 = 16;    // alignof(__int128) or max alignment
    dword_126E318 = 16;    // alignof(long double, repeated)
    dword_126E278 = 16;    // maximum natural alignment

    // === Endianness and signedness ===
    dword_126E4A4 = 1;     // little-endian
    dword_126E498 = 1;     // char is signed
    dword_126E368 = 1;     // int is 2's complement
    dword_126E384 = 1;     // enum underlying type signed

    // === Bit-field and struct layout ===
    dword_126E3A8 = -1;    // MSVC bit-field allocation mode (-1 = disabled)
    dword_126E2A8 = 0;     // no extra struct padding
    dword_126E2F0 = 0;     // field alignment override disabled
    dword_126E398 = 0;     // no special alignment for unnamed fields
    dword_126E298 = 0;     // no zero-length array as last field padding
    dword_126E288 = 1;     // field alloc order = declaration order
    dword_126E294 = 1;     // allow zero-sized objects
    dword_126E28C = 1;     // allow empty base optimization

    // === ABI flags ===
    dword_126E394 = 1;     // ELF-style name mangling
    dword_126E3AC = 1;     // Itanium ABI compliance
    dword_126E37C = 1;     // EH table generation enabled
    dword_126E3A0 = 0;     // no Windows SEH
    dword_126E36C = 1;     // thunks for virtual calls
    dword_126E380 = 1;     // covariant return types
    dword_126E39C = 0;     // no RTTI incompatibility workaround

    // === Integral type encoding (byte_126E4xx) ===
    byte_126E431 = 0;      // bool encoding index
    byte_126E430 = 2;      // char encoding index
    byte_126E480 = 4;      // char16_t encoding
    byte_126E470 = 6;      // char32_t encoding
    byte_126E490 = 5;      // wchar_t encoding
    byte_126E481 = 6;      // char8_t encoding

    // === Size_t properties ===
    byte_126E349 = 8;      // size_t byte width indicator
    qword_126E350 = -1;    // SIZE_MAX (0xFFFFFFFFFFFFFFFF for 64-bit)
    byte_126E348 = 7;      // size_t type encoding index

    // === String properties ===
    dword_126E49C = 8;     // host string char bit width
    dword_126E1BC = 1;     // feature flag (enabled)
    dword_126E494 = 1;     // null-terminated string assumption

    // === Replicated size values (qword versions) ===
    // These are 64-bit copies of the 32-bit size values above,
    // used for 64-bit arithmetic in constant evaluation
    qword_126E330 = 8;     // sizeof(long) as int64
    qword_126E340 = 4;     // sizeof(int) as int64
    qword_126E300 = 16;    // sizeof(long double) as int64
    qword_126E310 = 16;    // alignof(long double) as int64
    qword_126E418 = 4;     // sizeof(short) as int64
    qword_126E3E8 = 16;    // sizeof(__int128) as int64
    qword_126E408 = 8;     // sizeof(long long) as int64
    qword_126E320 = 16;    // alignof(something 16B) as int64
    qword_126E3F8 = 8;     // alignof(double) as int64
    qword_126E3D0 = 16;    // sizeof(max int) as int64
    qword_126E360 = 8;     // sizeof(long) alignment as int64
    qword_126E2C0 = 8;     // sizeof(pointer) as int64
    qword_126E2B0 = 16;    // alignof(pointer, packed) as int64
    qword_126E428 = 2;     // sizeof(wchar_t) as int64
    qword_126E2A0 = 8;     // sizeof(ptrdiff_t) as int64

    // === Miscellaneous ===
    qword_126E3B0 = 0;     // no custom va_list
    qword_126E3B8 = 0;     // no custom va_list secondary
    dword_126E3A4 = 0;     // bit-field container sizing disabled
    byte_126E2F6 = 4;      // unnamed struct alignment
    byte_126E2F5 = 4;      // unnamed union alignment
    byte_126E2F4 = 4;      // default minimum alignment
    byte_126E2F7 = 4;      // stack alignment
    byte_126E2F8 = 4;      // thread-local alignment
    byte_126E358 = 7;      // size_t type kind encoding
    dword_126E370 = 0;     // padding/zero
    dword_126E374 = 0;
    dword_126E378 = 1;     // 64-bit mode flag (LP64)
    dword_126E290 = 0;
    dword_126E388 = 0;
    dword_126E38C = 0;
    dword_126E390 = -1;    // special marker

    return -1;  // return value unused by caller
}
```

The function establishes the LP64 data model: `sizeof(int)=4`, `sizeof(long)=8`, `sizeof(pointer)=8`. This matches the CUDA device code ABI where device pointers are 64-bit. The `dword_126E378 = 1` flag explicitly marks this as 64-bit mode.

### CLI Overrides for the Data Model

Two CLI flags can override specific type properties set by `sub_7515D0`, because they are processed before case 245 in the switch:

**Case 65 (`--force-lp64`):** Enforces 64-bit pointer and long sizes:
```c
case 65:
    dword_106C01C = 1;          // force-lp64 flag recorded
    qword_126E408 = 8;          // sizeof(long long) = 8
    dword_126E400 = 8;          // alignof(long long) = 8
    byte_126E349 = 8;           // size_t = 8 bytes
    byte_126E358 = 7;           // size_t type encoding
```

**Case 66 (`--force-llp64`):** Sets 32-bit pointer and long sizes (Windows-like):
```c
case 66:
    dword_106C018 = 1;          // force-llp64 flag recorded
    qword_126E408 = 4;          // sizeof(long long) = 4
    dword_126E400 = 4;          // alignof(long long) = 4
    byte_126E349 = 10;          // size_t = different encoding
    byte_126E358 = 9;           // size_t type encoding
```

**Case 90 (`--m32`):** Sets the complete 32-bit (ILP32) data model:
```c
case 90:
    dword_126E378 = 0;          // 32-bit mode (not LP64)
    qword_126E360 = 4;          // sizeof(long) = 4
    dword_126E35C = 4;          // alignof(long) = 4
    qword_126E350 = 0xFFFFFFFF; // SIZE_MAX = 32-bit
    byte_126E349 = 6;           // size_t = 4 bytes
    byte_126E358 = 5;           // size_t type encoding
    qword_126E2C0 = 4;          // sizeof(pointer) = 4
    dword_126E2B8 = 4;          // sizeof(pointer, dword) = 4
    qword_126E2B0 = 8;          // alignof(pointer, packed) = 8
    dword_126E2AC = 4;          // alignof(pointer) = 4
    qword_126E2A0 = 4;          // sizeof(ptrdiff_t) = 4
    dword_126E29C = 4;          // sizeof(ptrdiff_t, dword) = 4
    qword_126E408 = 4;          // sizeof(long long) = 4
    dword_126E400 = 4;          // alignof(long long) = 4
    byte_126E2F4 = 4;           // default minimum alignment = 4
```

Because `sub_7515D0` is called from `sub_7525F0` (which runs during case 245), and case 90 executes before case 245, the `--m32` overrides are applied first but then overwritten by `sub_7515D0`'s LP64 defaults. This means the 32-bit overrides from `--m32` are effective ONLY for the globals that `sub_7515D0` does NOT touch. For the globals that both code paths write (like `qword_126E408`, `dword_126E400`, `byte_126E349`, `byte_126E358`), the `sub_7515D0` LP64 values take precedence. However, `--force-lp64` and `--force-llp64` are no-ops when `--target` is also specified, because `sub_7515D0` overwrites their values too.

In practice, nvcc controls all of these flags coherently -- it does not pass conflicting combinations.

### Configuration Validation (sub_7527B0)

After `sub_7515D0` sets the type tables, `sub_752DF0` (`init_field_alignment_tables`) populates alignment lookup tables and then calls `sub_7527B0` (`check_target_configuration`). This function validates the consistency of the configured type model:

```c
// sub_7527B0 -- check_target_configuration (pseudocode summary)
void check_target_configuration()
{
    // Validate char fits in 8 bytes
    compute_type_size(0, &size, &precision);
    if (size > 8) fatal("target char is too large");

    // Validate wchar_t size
    if (qword_126E488 > 8) fatal("target wchar_t is too large");

    // Validate char16_t: must be unsigned, at least 16 bits
    if (qword_126E478 > 8) fatal("target char16_t is too large");
    if (dword_126E4A0 * qword_126E478 <= 15)
        fatal("target char16_t is too small");
    if (is_unsigned[byte_126E480] == 0)
        assert_fail("target char16_t must be unsigned");

    // Validate char32_t: must be unsigned, at least 32 bits
    if (qword_126E468 > 8) fatal("target char32_t is too large");
    if (dword_126E4A0 * qword_126E468 <= 31)
        fatal("target char32_t is too small");
    if (is_unsigned[byte_126E470] == 0)
        assert_fail("target char32_t must be unsigned");

    // Validate size_t range
    compute_type_size(byte_126E349, &size, &precision);
    if (size * dword_126E4A0 > 64) size_bits = 64;
    if (qword_126E350 > max_for_bits(size_bits))
        fatal("targ_size_t_max is too large");

    // Validate largest integer type
    if (qword_126E3D8 > 16) fatal("targ_sizeof_largest_integer too large");
    if (qword_126E3D8 < qword_126E3F8)
        fatal("invalid targ_sizeof_largest_integer");

    // Validate INT_VALUE_PARTS
    if (16 * dword_126E4A0 != 128)
        fatal("invalid INT_VALUE_PARTS_PER_INTEGER_VALUE");

    // Validate host string char
    if (dword_126E49C > 8) fatal("targ_host_string_char_bit too large");

    // Validate pack alignment
    if (dword_126E284 < 1 || dword_126E284 > 255)
        fatal("invalid targ_minimum_pack_alignment");
    if (dword_126E284 > dword_126E280)
        fatal("invalid targ_maximum_pack_alignment");

    // Validate GNU IA-32 vector function integer sizes
    if (qword_126E428 != 2 || qword_126E418 != 4 || qword_126E3F8 != 8)
        assert_fail("invalid integer sizes for GNU IA-32 vector functions");

    // Validate MSVC bit-field allocation
    if (dword_126E3A4 && dword_126E3A8 != -1)
        fatal("targ_microsoft_bit_field_allocation must be -1 "
              "when targ_bit_field_container_size is TRUE");

    // Validate field allocation order
    if (!dword_126E3AC) assert_fail(...);
    if (!dword_126E288)
        fatal("targ_field_alloc_sequence_equals_decl_sequence must be TRUE");

    // Validate host/target endianness match
    if (dword_126E4A4 != dword_126EE40)
        fatal("unexpected host/target endian mismatch");

    // After validation, call dialect selector
    select_cp_gen_be_target_dialect();  // sub_752A80
}
```

The validator confirms that the type model is internally consistent. Most of these checks are compile-time assertions that should never fire with the hardcoded LP64 values from `sub_7515D0`, but they guard against corruption or misconfiguration if the type globals are modified by other code paths (such as `--m32` or `--force-llp64`).

Notably, `check_target_configuration` calls `select_cp_gen_be_target_dialect` (`sub_752A80`) as its last action. This means dialect selection happens after all type model validation is complete.

### Field Alignment Tables (sub_752DF0)

`init_field_alignment_tables` populates two alignment lookup tables at `qword_12C7640` and `qword_12C7680`. These tables map integer type kinds to their struct field alignment requirements. The function only fills the tables when `dword_126E2F0` (field alignment override) is nonzero; in the default CUDA configuration, this field is set to 0 by `sub_7515D0`, so the alignment tables remain at their initialized-to-zero state.

When the tables are populated, they read alignment values from the `dword_126E2CC`-`dword_126E2F0` range (which `sub_7515D0` leaves at zero), meaning the alignment tables are effectively disabled for CUDA targets. The function also copies `qword_126E3E8` (sizeof largest integer type) into `qword_126E3D8` before calling the configuration validator.

## Stage 4: Dialect Selection (sub_752A80)

`select_cp_gen_be_target_dialect` determines whether the backend generates device-side or host-side C++ output. It is called from `check_target_configuration` (`sub_7527B0`) after all type model validation passes:

```c
// sub_752A80 -- select_cp_gen_be_target_dialect (complete decompilation)
__int64 sub_752A80()
{
    // Guard: no dialect should be set yet
    if (dword_126E1F8 || dword_126E1D0 || dword_126E1FC || dword_126E1E8)
        assert_fail("target.c", 736,
                    "select_cp_gen_be_target_dialect",
                    "Target dialect already set.", 0);

    if (dword_126EFA8) {           // GPU compilation mode enabled
        dword_126E1DC = 1;         // enable cp_gen backend
        dword_126E1EC = 1;         // enable backend output

        if (dword_126EFA4) {       // device-side compilation
            dword_126E1E8 = 1;     // set device target dialect
            qword_126E1E0 = qword_126EF90;  // copy Clang version
            return qword_126EF90;
        } else {                   // host-side compilation (stub generation)
            dword_126E1F8 = 1;     // set host target dialect
            qword_126E1F0 = qword_126EF98;  // copy GCC version
            return qword_126EF98;
        }
    }
    return result;  // non-GPU mode: no dialect set
}
```

The guard at entry checks that no dialect has been previously set. This fires only if `select_cp_gen_be_target_dialect` is called twice, which is a programming error.

### Dialect Global Roles

| Global | Role | Set When |
|---|---|---|
| `dword_126EFA8` | GPU compilation mode active | `--gcc` flag (case 182) sets this to 1 |
| `dword_126EFA4` | Device-side (vs host-side) compilation | `--clang` flag (case 187) sets this to 1 |
| `dword_126E1DC` | cp_gen backend enabled | GPU mode active |
| `dword_126E1EC` | Backend output enabled | GPU mode active |
| `dword_126E1E8` | Device target dialect selected | Device-side compilation |
| `dword_126E1F8` | Host target dialect selected | Host-side compilation |
| `qword_126E1E0` | Device compiler version | Copied from `qword_126EF90` (Clang version) |
| `qword_126E1F0` | Host compiler version | Copied from `qword_126EF98` (GCC version) |

The naming of `dword_126EFA8` as "gcc mode" and `dword_126EFA4` as "clang mode" is misleading. In CUDA compilation, `dword_126EFA8` means "GPU compilation is active" (nvcc always passes `--gcc`) and `dword_126EFA4` means "this is the device-side pass" (nvcc passes `--clang` for the device compilation pass, not for the host pass). The version numbers copied into `qword_126E1E0` and `qword_126E1F0` represent the host compiler's version for pragma compatibility, not the "Clang" or "GCC" version in any semantic sense.

### Device vs Host Output Paths

cudafe++ is invoked twice per `.cu` file by nvcc:

1. **Device pass** (`dword_126EFA4 = 1`): cudafe++ processes the CUDA source and emits the device-side IL/PTX code. The dialect is set to "device" (`dword_126E1E8 = 1`) and the version number comes from `qword_126EF90`.

2. **Host pass** (`dword_126EFA4 = 0`): cudafe++ processes the same source and emits the host-side `.int.c` file with device stubs. The dialect is set to "host" (`dword_126E1F8 = 1`) and the version number comes from `qword_126EF98`.

The dialect selection determines which backend code paths execute during `.int.c` generation. Device-dialect mode generates PTX-compatible output; host-dialect mode generates host C++ with stub functions.

## Stage 5: TU Initialization (sub_586240)

During translation unit initialization, `sub_586240` copies the target index from the CLI-level global into per-TU state:

```c
// sub_586240 -- fe_translation_unit_init_secondary (relevant excerpt)
if (dword_106BA08) {                       // is recompilation / secondary TU
    // ... version marker and timestamp setup ...
    v6 = allocate(4);
    *(int32_t *)v6 = 3550774;              // "6.6\0" -- EDG version marker
    qword_126EB78 = v6;                    // store version string pointer
    qword_126EB80 = strcpy(allocate(len), byte_106B5C0);  // timestamp
    dword_126EBF8 = dword_126E4A8;         // CRITICAL: copy target index
}
```

The copy `dword_126EBF8 = dword_126E4A8` replicates the architecture index into the translation unit's state block. Both globals contain the same value in single-TU compilation (which is the only mode CUDA uses). The dual-variable pattern exists because EDG's multi-TU architecture theoretically supports per-TU target configurations, but CUDA compilation always uses a single target per cudafe++ invocation.

After this point, feature checks throughout the compiler read either `dword_126E4A8` (the CLI-level global) or `dword_126EBF8` (the TU-level copy). Both contain the same integer target index.

## Feature Gate Mechanism

Individual features are gated by comparing `dword_126E4A8` against threshold constants during semantic analysis. The pattern is consistent across all architecture-gated features:

```c
// Pattern: hard error on unsupported architecture
if (dword_126E4A8 < THRESHOLD) {
    emit_error(DIAGNOSTIC_ID, location);
    // compilation continues or aborts depending on severity
}
```

Some features use a global flag that is set during target initialization rather than reading `dword_126E4A8` directly. For example, `__nv_register_params__` checks `dword_106C028` (the "uumn" flag, set by CLI case 112) rather than comparing the architecture directly:

```c
// sub_40B0A0 -- apply_nv_register_params_attr
if (!dword_106C028) {                    // feature not enabled
    emit_error(7, 3659, location);       // "not enabled" error
    v3 = 0;                              // mark as invalid
}
```

The architecture check for `__nv_register_params__` is separate -- it uses diagnostic tag `register_params_unsupported_arch` (requiring compute_80+), which is evaluated in a different code path from the enable flag check.

### Feature Flag vs Direct Comparison

The distinction between feature-flag gating and direct SM comparison is:

- **Direct comparison** (`dword_126E4A8 < N`): Used for features where the threshold is baked into the comparison instruction. The threshold cannot be changed without recompiling cudafe++. Examples: `__grid_constant__` (< 70), `__managed__` (< 30), `alloca()` (< 52).

- **Feature flag** (`dword_XXXXXX == 0`): Used for features that can be enabled/disabled independently of the architecture. The flag is set by a CLI option, and the architecture is checked separately. Example: `__nv_register_params__` uses `dword_106C028` for the enable check and a separate comparison for the architecture check.

Both patterns ultimately depend on the target index value, but the feature-flag pattern adds an extra level of indirection that allows nvcc to control feature availability through CLI flags rather than relying solely on the architecture number.

## The --db Debug Mechanism

The `--db` flag (CLI case 37) activates EDG's internal debug tracing. While not directly part of the architecture detection chain, it shares adjacent globals (`dword_126EFC8`, `dword_126EFCC`) and can expose architecture checks as they execute.

The `--db` flag calls `sub_48A390` (`proc_debug_option`, 238 lines, `debug.c`). On entry, it unconditionally enables tracing:

```c
dword_126EFC8 = 1;  // debug tracing enabled
```

If the argument is a bare integer, it sets the verbosity level:

```c
if (first_char is digit) {
    dword_126EFCC = strtol(arg, NULL, 10);  // verbosity level
    return 0;
}
```

Otherwise, it parses debug trace control entries (see [Architecture Feature Gating](../cuda/arch-gating.md) for the full `--db` parsing grammar). After `proc_debug_option` returns, the CLI parser saves the verbosity level:

```c
// proc_command_line, case 37
dword_106C2A0 = dword_126EFCC;  // save error count baseline
```

At higher verbosity levels (5+), the compiler logs IL tree walking with messages like `"Walking IL tree, entry kind = ..."`, which provides visibility into when architecture gate checks fire during semantic analysis.

## Complete Call Graph

```text
main (sub_585EE0)
  |
  +-> proc_command_line (sub_459630)
  |     |
  |     +-> case 90 (--m32):     set ILP32 type properties
  |     +-> case 65 (--force-lp64): set LP64 overrides
  |     +-> case 66 (--force-llp64): set LLP64 overrides
  |     +-> case 245 (--target):
  |           |
  |           +-> sub_7525E0(<arg>)            // parse target index (stub: returns -1)
  |           +-> dword_126E4A8 = result       // store target index
  |           +-> if -1: emit error 2664       // invalid target
  |           +-> sub_7525F0(result)           // set_target_configuration
  |                 |
  |                 +-> sub_7515D0()           // initialize 100+ type globals (LP64)
  |                 +-> qword_126E1B0 = "lib"  // library prefix
  |                 |
  |                 +-> [implicit via sub_752DF0]:
  |                       +-> sub_752DF0()     // init_field_alignment_tables
  |                       +-> sub_7527B0()     // check_target_configuration
  |                             |
  |                             +-> [20+ consistency checks]
  |                             +-> sub_752A80()   // select_cp_gen_be_target_dialect
  |                                   |
  |                                   +-> if GPU mode && device:
  |                                   |     dword_126E1E8 = 1 (device dialect)
  |                                   |     qword_126E1E0 = qword_126EF90
  |                                   +-> if GPU mode && host:
  |                                         dword_126E1F8 = 1 (host dialect)
  |                                         qword_126E1F0 = qword_126EF98
  |
  +-> fe_translation_unit_init (sub_586240)
        |
        +-> dword_126EBF8 = dword_126E4A8      // copy target index to TU state
        +-> qword_126EB78 = "6.6\0"            // EDG version marker
        +-> qword_126EB80 = timestamp           // compilation timestamp

[After TU init, feature checks read dword_126E4A8 or dword_126EBF8]
```

## Global Variable Summary

### Target Architecture State

| Address | Size | Name | Role |
|---|---|---|---|
| `dword_126E4A8` | 4 | `sm_architecture` | Target index from `--target`. Sentinel: `-1`. |
| `dword_126EBF8` | 4 | `target_configuration_index` | TU-level copy of `dword_126E4A8`. |
| `dword_126E378` | 4 | `is_64bit_mode` | 1 = LP64 (64-bit), 0 = ILP32 (32-bit). |
| `dword_126E4A4` | 4 | `target_little_endian` | 1 = little-endian. |

### Type Model (Sizes, set by sub_7515D0)

| Address | Size | Name | LP64 Value |
|---|---|---|---|
| `dword_126E338` / `qword_126E340` | 4/8 | `sizeof_int` | 4 |
| `dword_126E328` / `qword_126E330` | 4/8 | `sizeof_long` | 8 |
| `dword_126E2B8` / `qword_126E2C0` | 4/8 | `sizeof_pointer` | 8 |
| `dword_126E29C` / `qword_126E2A0` | 4/8 | `sizeof_ptrdiff` | 8 |
| `dword_126E410` / `qword_126E418` | 4/8 | `sizeof_short` | 4 |
| `dword_126E400` / `qword_126E408` | 4/8 | `sizeof_long_long` | 8 |
| `dword_126E420` / `qword_126E428` | 4/8 | `sizeof_wchar` | 2 |
| `dword_126E2FC` / `qword_126E300` | 4/8 | `sizeof_long_double` | 16 |
| `dword_126E258` | 4 | `double_mantissa_bits` | 53 |
| `dword_126E264` | 4 | `float_mantissa_bits` | 24 |
| `dword_126E234` | 4 | `float128_mantissa_bits` | 113 |

### Type Model (Alignment, set by sub_7515D0)

| Address | Size | Name | LP64 Value |
|---|---|---|---|
| `dword_126E2AC` | 4 | `alignof_pointer` | 8 |
| `dword_126E35C` / `qword_126E360` | 4/8 | `alignof_long` | 8 |
| `dword_126E308` / `qword_126E310` | 4/8 | `alignof_long_double` | 16 |
| `dword_126E3F0` / `qword_126E3F8` | 4/8 | `alignof_double` | 8 |
| `dword_126E278` | 4 | `max_natural_alignment` | 16 |
| `byte_126E2F4` | 1 | `default_min_alignment` | 4 |

### Dialect Selection State

| Address | Size | Name | Role |
|---|---|---|---|
| `dword_126EFA8` | 4 | `gpu_mode_enabled` | GPU compilation active (set by `--gcc`) |
| `dword_126EFA4` | 4 | `is_device_compilation` | Device-side pass (set by `--clang`) |
| `dword_126E1DC` | 4 | `cp_gen_enabled` | cp_gen backend active |
| `dword_126E1EC` | 4 | `backend_output_enabled` | Backend output generation active |
| `dword_126E1E8` | 4 | `device_dialect_set` | Device target dialect selected |
| `dword_126E1F8` | 4 | `host_dialect_set` | Host target dialect selected |
| `qword_126E1E0` | 8 | `device_version` | Clang version copied for device dialect |
| `qword_126E1F0` | 8 | `host_version` | GCC version copied for host dialect |
| `qword_126E1B0` | 8 | `lib_prefix` | Library search prefix, set to `"lib"` |

### Feature Gate Globals

| Address | Size | Name | Role |
|---|---|---|---|
| `dword_106C028` | 4 | `nv_register_params_enabled` | Enable flag for `__nv_register_params__` (set by `--uumn`, case 112) |

## Cross-References

- [CLI Flag Inventory](./cli-flags.md) -- `--target` (case 245), `--m32` (case 90), `--force-lp64` (case 65), `--force-llp64` (case 66) flag details
- [Architecture Feature Gating](../cuda/arch-gating.md) -- SM version thresholds for CUDA features, host compiler version gating, `--db` debug mechanism
- [EDG Build Configuration](./edg-build-config.md) -- Compile-time constants controlling backend selection and IL configuration
- [Pipeline Overview](../pipeline/overview.md) -- Where architecture detection fits in the compilation pipeline
- [CLI Processing](../pipeline/cli.md) -- `proc_command_line` dispatcher and flag table mechanics
- [Translation Unit Descriptor](../structs/translation-unit.md) -- TU state block containing `dword_126EBF8`
- [Global Variable Index](../reference/global-variables.md) -- Full address-level documentation of all globals
- [Minor Attributes](../attributes/minor-attributes.md) -- `__nv_register_params__` attribute handler and `dword_106C028` usage
