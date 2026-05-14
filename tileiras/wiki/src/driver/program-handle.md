# Driver Program Handle

## Abstract

A single 104-byte program handle represents one TileIR translation unit in the tileiras driver. The public C-API surface exposes only an opaque pointer, but the recovered allocation is a fixed 0x68-byte block built by `sub_57A480` (`tileirasProgramCreate`) and consumed by `sub_57A8E0` (`tileirasProgramCompile`), `sub_57A850` (`tileirasProgramGetOutput`), and `sub_57A7C0` (`tileirasProgramRelease`). Every offset is reachable from the four public entry points, and the layout stays stable across the three create/compile/release call sites in the driver binary.

The handle stores the validated driver configuration, a small ownership bit, and an inline byte view that doubles as a CUDA-root pointer during early lifetime and as the compiled-output byte span after compile. Storage at `+0x48` lives a two-phase life: the slot holds the resolved CUDA install root while the front end runs, then the same 16 bytes are repurposed to track the compiled output buffer once `sub_57A8E0` finishes.

## Public Error Codes

Every entry point in the driver's public C API returns a small integer status. Five non-zero codes are emitted across the four functions, and every diagnostic routes through `sub_578D40` with a packed severity byte. The severity values 259, 260, and 2563 are the `(class | (op_prefix << 8) | (trace << 9))` form documented in [Diagnostic ABI and Helpers](../mlir-infra/diagnostic-abi-and-helpers.md#severity-word): 259 and 260 are fatal driver errors, 2563 is a user-input rejection.

| Code | Trigger | Verbatim diagnostic | Severity |
|---|---|---|---|
| 0 | success | (none) | — |
| 1 | allocation failure (`sub_44A8C20(0x68)` returns NULL) | `failed to allocate memory for program` | 259 |
| 2 | null `config` (`out_program` ok) | `configuration is null` | 2563 |
| 2 | null `inputBuffer` | `input buffer is null` | 2563 |
| 2 | `opt_level != 0 && device_debug == 1` | `optimized debugging is not supported, change optimization level to 0 or disable full debug info` | 2563 |
| 2 | invalid GPU id (not in `{100, 103, 110, 120, 121}`) | `unsupported GPU target` | 2563 |
| 2 | `opt_level > 3` | `invalid optimization level` | 2563 |
| 2 | unsupported host arch (not in `{0, 1, 2}`) | `unsupported host architecture` | 2563 |
| 2 | unsupported host OS (not in `{0, 1}`) | `unsupported host operating system` | 2563 |
| 3 | parse failure on TileIR bytecode magic | `input does not correspond to Tile IR bytecode` | 260 |
| 3 | parse failure with MLIR fall-through | `failed to parse IR bytecode (it looks like MLIR bytecode instead)` | 260 |
| 4 | null `program` (`Compile`, `GetOutput`, `Release`) | `program is null` | 2563 |
| 4 | null output pointer (`GetOutput`) | `output pointer is null` | 2563 |
| 4 | output requested before compile (`GetOutput`) | `program has not been compiled` | 2563 |
| 5 | compile failure (`tileirasProgramCompile`) | `failed to compile Tile IR program` | 259 |

Code 1 is reserved for the single allocation site at `sub_44A8C20(0x68)`. Code 2 covers every configuration-level rejection. Code 3 is reserved for bytecode-parse failures and is the only code with a conditional suffix appended by a magic-tail heuristic. Code 4 is the shared null-handle / not-compiled rejection used by every entry point other than create. Code 5 is the compile-time failure code emitted by `sub_57A8E0`.

The MLIR fall-through on code 3 is a small heuristic inside `sub_57A480`. When the bytecode magic check fails, the function scans the input for the 8-byte ASCII tail `e IR byt` — the suffix of `MLIR bytecode` minus the leading `ML ` — and on a match appends ` (it looks like MLIR bytecode instead)` to the base code-3 diagnostic so the user can route their bytecode to the right tool.

## tileirasProgramCreate Validation Order

`sub_57A480` is an 820-byte routine that funnels every diagnostic through `sub_578D40`. Validation order is fixed and observable from the call sites — a caller can rely on each check happening before the next, because every early return is a separate diagnostic with a separate severity field.

```c
int tileirasProgramCreate(TileirasProgram **out_program, const TileirasConfig *config) {
    if (out_program == NULL)                                    return 4;  // "program is null"
    if (config == NULL)                                         return 2;  // "configuration is null"
    if (config->input_buffer_data == NULL)                      return 2;  // "input buffer is null"
    if (!sub_57FF40(config->input_buffer_data,
                    config->input_buffer_size))                 return 3;  // parse probe; MLIR tail check appends suffix
    if (!is_supported_gpu(config->gpu_id))                      return 2;  // "unsupported GPU target"
    if ((uint32_t)config->opt_level > 3)                        return 2;  // "invalid optimization level"
    if (config->opt_level != 0 && config->device_debug == 1)    return 2;  // "optimized debugging is not supported, ..."
    if ((uint32_t)config->host_arch > 2)                        return 2;  // "unsupported host architecture"
    if ((uint32_t)config->host_os > 1)                          return 2;  // "unsupported host operating system"

    TileirasProgram *p = (TileirasProgram *)sub_44A8C20(0x68);
    if (p == NULL)                                              return 1;  // "failed to allocate memory for program"

    copy_config_into_handle(p, config);
    p->owning_flag = 0;
    *out_program = p;
    return 0;
}
```

The eight predicate gates are pure functions of the caller's argument tuple. Only after every predicate passes does `sub_57A480` request the 104-byte block from the allocator. Allocation is the last possible failure point, so a successful return from `tileirasProgramCreate` guarantees the handle is initialized end-to-end.

## tileirasConfig Layout

The configuration is a 16-byte aligned block whose first 80 bytes mirror the front of the program handle. `sub_57A480` reads it through five `_mm_loadu_si128` loads plus one scalar slot, pinning the layout to exactly five 16-byte rows.

```c
typedef struct TileirasConfig {
    /*+0x00*/ const void *input_buffer_data;     // bytecode bytes
    /*+0x08*/ size_t      input_buffer_size;     // bytes in buffer
    /*+0x10*/ int32_t     gpu_id;                // 100/103/110/120/121 (sub_57A450 whitelist)
    /*+0x14*/ int32_t     opt_level;             // 0..3
    /*+0x18*/ int32_t     device_debug;          // 0 or 1
    /*+0x1C*/ int32_t     lineinfo;              // 0 or 1
    /*+0x20*/ int32_t     host_arch;             // 0=x86_64, 1=aarch64, 2=arm64ec
    /*+0x24*/ int32_t     host_os;               // 0=linux, 1=windows
    /*+0x28*/ int32_t     sanitize;              // 0=off, 1=memcheck
    /*+0x2C*/ uint32_t    pad_2C;
    /*+0x30*/ /* std::string SSO */              // output file name (driver side)
    /*+0x40*/ const char *cuda_root_ptr;         // resolved by sub_5773C0
    /*+0x48*/ size_t      cuda_root_len;
} TileirasConfig;
```

The fields at `+0x10..+0x28` are the validated driver options. The CUDA root string at `+0x40` is the resolution of the `CUDA_ROOT` / `CUDA_HOME` / `CUDA_PATH` environment chain (with `/proc/self/exe` fallback) performed by `sub_5773C0`; tileiras carries it into the program handle because the compile dispatch needs an installation path to invoke `nvdisasm`.

## TileirasProgram Layout

The 104-byte program handle reuses the first 80 bytes of the configuration almost verbatim, with one deliberate field reorder: `gpu_id` moves to `+0x28` so the validated 32-bit fields at `+0x10..+0x28` form a contiguous scalar block that the compile dispatch reads via aligned 32-bit loads.

```c
typedef struct TileirasProgram {
    /*+0x00*/ const void *input_buffer_data;     // ptr to bytecode bytes
    /*+0x08*/ size_t      input_buffer_size;     // bytes in buffer
    /*+0x10*/ int32_t     opt_level;             // 0..3
    /*+0x14*/ int32_t     device_debug;          // 0 or 1
    /*+0x18*/ int32_t     lineinfo;              // 0 or 1
    /*+0x1C*/ int32_t     host_arch;             // 0=x86_64, 1=aarch64, 2=arm64ec
    /*+0x20*/ int32_t     host_os;               // 0=linux, 1=windows
    /*+0x24*/ int32_t     sanitize;              // 0 or 1 (memcheck)
    /*+0x28*/ int32_t     gpu_id;                // 100/103/110/120/121
    /*+0x2C*/ uint32_t    pad_2C;
    /*+0x40*/ const char *cuda_root_ptr;         // resolved at compile time
    /*+0x48*/ size_t      cuda_root_len;         // ── overlapping slot ──
    /*+0x48*/ uint8_t    *output_data;           // SSO: same 16 bytes, second life
    /*+0x50*/ uint64_t    output_capacity;
    /*+0x58*/ size_t      output_length;
    /*+0x60*/ uint32_t    owning_flag;           // 1 = handle owns output_data
} TileirasProgram;
```

Five unaligned SSE copies from the configuration populate the block. The first copy moves `input_buffer_data` and `input_buffer_size`; the next two move the eight 32-bit option fields; the fourth clears the slot at `+0x30`; the fifth installs the CUDA-root SSO. `owning_flag` at `+0x60` clears to zero before `sub_57A480` returns, so the handle starts life with no output buffer to free.

## SSO Overlap at +0x48

The slot at `+0x48` is the only address in the handle that hosts two different fields across the program lifetime. While the front end runs, `+0x40..+0x4F` carries a `(cuda_root_ptr, cuda_root_len)` pair pointing at the resolved CUDA install path. The compile dispatcher reads the pair once, uses it to locate the `nvdisasm` binary, and never touches it again. The same 16 bytes are then overwritten to hold the compiled-output buffer descriptor: `output_data` at `+0x48`, `output_capacity` at `+0x50`, `output_length` at `+0x58`.

The offsets used by the consumers make this observable. `sub_57A8E0` writes `output_data` at `+72` (`0x48`), `output_capacity` at `+80` (`0x50`), and `output_length` at `+88` (`0x58`). `sub_57A850` returns `{ptr=+0x48, length=+0x58}` to the caller. The lifetimes never overlap: by the time `sub_57A8E0` installs the output bytes, the CUDA-root string has already been consumed and is no longer needed by any subsequent stage.

A reimplementation should keep this overlap as an internal storage trick and never expose it to callers. The public contract is simply that the program-output getter is invalid until compile has succeeded.

## Ownership and Release

A single bit at `+0x60`, `owning_flag`, controls release behavior. It starts at 0 immediately after `tileirasProgramCreate`. `sub_57A8E0` sets it to 1 once the compiled byte buffer has landed at `+0x48..+0x58`. `sub_57A7C0` (`tileirasProgramRelease`) reads the flag before tearing down: 1 makes the release path call the buffer's deleter on `output_data`; 0 leaves the slot alone. Either way, the 104-byte handle itself is freed via the matching `sub_4580C60` deallocator after the conditional output free.

The ownership rule means calling `tileirasProgramRelease` on a never-compiled handle is safe and touches no output memory. It also means the only legal way to retain compiled bytes after release is to copy them out via `tileirasProgramGetOutput` first.

## Public-API Surface

Four C-API entry points operate on the handle. Each one is a small wrapper that validates its arguments, walks a fixed offset path, and routes diagnostics through `sub_578D40`.

| Symbol | Identity | Role |
|---|---|---|
| `sub_57A480` | `tileirasProgramCreate` | Validates `(out_program, config, inputBuffer)`, allocates 0x68 bytes via `sub_44A8C20`, copies the configuration body, clears `owning_flag`. |
| `sub_57A8E0` | `tileirasProgramCompile` | Reads option fields at `+0x10..+0x28`, runs the compile dispatcher, writes the output buffer at `+0x48..+0x58`, sets `owning_flag` at `+0x60` to 1. |
| `sub_57A850` | `tileirasProgramGetOutput` | Returns `{ptr = *(uint8_t**)(handle+0x48), length = *(size_t*)(handle+0x58)}`; emits `program has not been compiled` (code 4) if `owning_flag` is 0. |
| `sub_57A7C0` | `tileirasProgramRelease` | If `owning_flag` is 1, frees `output_data`; then frees the 104-byte handle via `sub_4580C60`. |

All four entry points share the same C-API error space. The diagnostic emitter at `sub_578D40` is the single sink for every public message, which is why codes overlap across functions: code 4 covers `program is null`, `output pointer is null`, and `program has not been compiled` from the getter and release sites, while code 5 is reserved for the compile-time failure path inside `sub_57A8E0`.

## Lifecycle

The normal lifetime runs create → compile → get output → release, and the driver's `main` follows that sequence exactly. The handle is opaque at the public surface; the offset layout above is an implementation detail that reimplementers should reproduce for binary compatibility with the recovered driver but should never surface to consumers.

```c
TileirasProgram *program = NULL;
int err = tileirasProgramCreate(&program, &config);
if (err == 0) {
    err = tileirasProgramCompile(program);
}
if (err == 0) {
    TileirasByteView out;
    err = tileirasProgramGetOutput(program, &out);
    if (err == 0) {
        write_output_file(opts.output_path, out.data, out.length);
    }
}
tileirasProgramRelease(program);
```

Compile is allowed to fail after a successful create. When it does, `sub_57A8E0` returns code 5 (`failed to compile Tile IR program`), `owning_flag` stays 0, the output slot still holds the CUDA-root pair, and release frees only the handle. The getter rejects subsequent calls with code 4 (`program has not been compiled`), preserving the invariant that a returned byte view stays valid until the next mutating call on the handle.

## Reimplementation Invariants

The handle is 104 bytes. The option fields at `+0x10..+0x28` are eight contiguous 32-bit integers in the order `(opt_level, device_debug, lineinfo, host_arch, host_os, sanitize, gpu_id, pad)`. The slot at `+0x48..+0x57` is a single 16-byte region with two sequential lifetimes — CUDA-root SSO during the front end, output-buffer descriptor afterwards. The byte at `+0x60` is the ownership flag, the only field `tileirasProgramRelease` inspects when deciding whether to free the output buffer. Allocation goes through `sub_44A8C20(0x68)` and deallocation through `sub_4580C60`; no intermediate reallocation occurs on the create or compile paths.

## Cross-References

[Driver main() Entry](main-entry.md#main) documents how the handle is threaded through the four-phase driver and how the configuration is built from `cl::opt` storage. The compile dispatcher that mutates the handle is described in the [TileIR Pipeline Overview](../pipeline/overview.md#full-cascade).
