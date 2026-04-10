# Entity Node Layout

The entity node is the central data structure in cudafe++ (EDG 6.6) for representing every named declaration: functions, variables, fields, parameters, namespaces, and types. Each node is a variable-size record -- routines occupy 288 bytes, variables 232 bytes, fields 176 bytes -- linked into scope chains and cross-referenced by type nodes, expression nodes, and template instantiation records.

This page focuses on the CUDA-specific fields that NVIDIA grafted onto the EDG entity node. These fields encode execution space (`__host__`/`__device__`/`__global__`), variable memory space (`__shared__`/`__constant__`/`__managed__`), launch configuration (`__launch_bounds__`/`__cluster_dims__`/`__block_size__`/`__maxnreg__`), and assorted kernel metadata. The attribute application functions in `attribute.c` write these fields; the backend code generator, cross-space validator, IL walker, and stub emitter read them.

## Key Facts

| Property | Value |
|---|---|
| Routine entity size | 288 bytes (IL entry kind 11) |
| Variable entity size | 232 bytes (IL entry kind 7) |
| Field entity size | 176 bytes (IL entry kind 8) |
| Execution space offset | `+182` (1 byte, bitfield) |
| Memory space offset | `+148` (1 byte, bitfield) |
| Launch config pointer | `+256` (8-byte pointer to 56-byte struct) |
| Source file | `attribute.c` (writers), `nv_transforms.c` / `cp_gen_be.c` (readers) |
| Attribute dispatch | `sub_413240` (`apply_one_attribute`, 585 lines) |
| Post-validation | `sub_6BC890` (`nv_validate_cuda_attributes`) |

## Visual Layout (Routine Entity, 288 Bytes)

```
Offset   0         8        16        24        32        40        48        56
       +=========+=========+=========+=========+=========+=========+=========+=========+
  0x00 | next_entity_ptr   | name_string_ptr   |            (EDG internal)             |
       +---------+---------+---------+---------+---------+---------+---------+---------+
  0x20 |                              (EDG internal continued)                          |
       +---------+---------+---------+---------+---------+---------+---------+---------+
  0x40 |                              (EDG internal continued)                          |
       +====+====+=========+=========+=========+=========+=========+=========+=========+
  0x50 |kind|stor|         | assoc_entity_ptr  |                                       |
       |+80 |+81 |         |                   |                                       |
       +----+----+---------+---------+---------+---------+---------+---------+---------+
  0x60 |                   | variable_type_ptr |                                       |
       +=========+=========+=========+=========+====+=========+=========+==========+===+
  0x80 | storage_class/align|         |type_kind|    | return_type_ptr   |MEM |EXT |    |
       |         |         |         |+132     |    | +144              |+148|+149|    |
       +---------+---------+---------+----+----+----+---------+---------+----+----+----+
  0x98 | proto_ptr / param_list +152  |link|stor|    |grid|    |op  |         |         |
       |                              |+160|+161|    |+164|    |+166|         |         |
       +---------+---------+---------+----+----+----+----+----+----+---------+---------+
  0xB0 |mbr |dev |    |kern|func|    |EXEC|CEXT| template_linkage_flags +184            |
       |+176|+177|    |+179|+180|    |+182|+183|                                       |
       +----+----+----+----+----+----+----+----+=========+=========+=========+=========+
  0xC0 | alias_chain/linkage+186      |         |ctor/dtor|lambda  |                    |
       |                              |         |  +190   | +191   |                    |
       +---------+---------+---------+---------+---------+---------+---------+---------+
  0xD0 | variable_alias_chain_next +208         |                                       |
       +---------+---------+---------+---------+---------+---------+---------+---------+
  0xF0 | func_extra / alias_entry +240          |                                       |
       +---------+---------+---------+---------+---------+---------+---------+---------+
 0x100 | LAUNCH_CONFIG_PTR +256      |         (padding to 288)                         |
       +=========+=========+=========+=========+=========+=========+=========+=========+

CUDA-specific fields (UPPERCASE):
  MEM   = +148  variable memory space bitfield (__device__/__shared__/__constant__)
  EXT   = +149  extended memory space (__managed__)
  EXEC  = +182  execution space bitfield (__host__/__device__/__global__)
  CEXT  = +183  CUDA extended flags (__nv_register_params__, __cluster_dims__ intent)
  LAUNCH_CONFIG_PTR = +256  pointer to 56-byte launch_config_t struct
```

## Full Offset Map (CUDA-Relevant Fields)

The table below documents every entity node offset touched by CUDA attribute handlers and validation functions. Offsets are byte positions from the start of the entity node. Fields marked "EDG base" are standard EDG fields that CUDA code tests but does not define.

| Offset | Size | Field | Set By | Read By |
|---|---|---|---|---|
| `+0` | 8 | Next entity pointer (linked list) | EDG | Scope iteration |
| `+8` | 8 | Name string pointer | EDG | Error messages, stub emission |
| `+80` | 1 | Entity kind byte (7=variable, 8=field, 11=routine) | EDG | All attribute handlers |
| `+81` | 1 | Storage flags (bit 2=local, bit 3=has_name, bit 6=anonymous) | EDG | `__global__` / `__device__` validation |
| `+88` | 8 | Associated entity pointer | EDG | `nv_is_device_only_routine` |
| `+112` | 8 | Variable type pointer | EDG | `get_func_type_for_attr` |
| `+128` | 1 | Storage class code / alignment | EDG | `apply_internal_linkage_attr` |
| `+132` | 1 | Type kind byte (12=qualifier) | EDG | Return type traversal |
| `+144` | 8 | Return type / next-in-chain pointer | EDG | `__global__` void-return check |
| `+148` | 1 | **Variable memory space bitfield** | CUDA attr handlers | Backend, IL walker |
| `+149` | 1 | **Extended memory space** | `apply_nv_managed_attr` | Backend, runtime init |
| `+152` | 8 | Function prototype / parameter list head | EDG | `__global__` param checks |
| `+160` | 1 | Linkage/visibility bits (variable: low 3 = visibility) | Various | Visibility propagation |
| `+161` | 1 | Storage/linkage flags (bit 7=thread_local) | EDG | `__managed__` / `__device__` validation |
| `+164` | 1 | Storage class / grid_constant flags (bit 2=grid_constant) | `__grid_constant__` handler | `__managed__`/`__device__` conflict check |
| `+166` | 1 | **Operator function kind** (5=operator function) | EDG | `__global__` validation |
| `+176` | 1 | Member function flags (bit 7=static member) | EDG | `__global__` static-member check |
| `+177` | 1 | Device propagation flag (bit 4=0x10) | Virtual override propagation | Override space checking |
| `+179` | 1 | **Constexpr/kernel flags** | Declaration processing | Stub generation, attribute interaction |
| `+180` | 1 | Function attributes (bit 6=nodiscard, bit 7=noinline) | Various attribute handlers | Backend |
| `+182` | 1 | **Execution space bitfield** | CUDA execution space handlers | Everywhere |
| `+183` | 1 | **CUDA extended flags** | `__cluster_dims__` / `__nv_register_params__` | Post-validation, stub emission |
| `+184` | 8 | **Template/linkage flags** (48-bit field) | EDG + CUDA handlers | Lambda check, visibility |
| `+186` | 1 | Alias chain flag (bit 3=internal linkage) | `apply_internal_linkage_attr` | Linker |
| `+190` | 1 | Constructor/destructor priority flags | `apply_constructor_attr` / `apply_destructor_attr` | Backend |
| `+191` | 1 | Lambda flags (bit 0=is_lambda) | EDG lambda processing | `__global__` validation |
| `+208` | 8 | Variable alias chain next pointer | `apply_alias_attr` | Alias loop detection |
| `+240` | 8 | Function extra info / alias entry | `apply_alias_attr` | Alias chain traversal |
| `+256` | 8 | **Launch configuration pointer** | CUDA launch config handlers | Post-validation, backend |

## Execution Space Bitfield (Byte +182)

This is the most frequently read field in CUDA-specific code paths. Every function entity carries a single byte that encodes which execution spaces the function belongs to.

```
Byte at entity+182:

  bit 0  (0x01)   device_capable     Function can execute on device
  bit 1  (0x02)   device_explicit    __device__ was explicitly written
  bit 2  (0x04)   host_capable       Function can execute on host
  bit 3  (0x08)   (reserved)
  bit 4  (0x10)   host_explicit      __host__ was explicitly written
  bit 5  (0x20)   device_annotation  Secondary device flag (HD detection)
  bit 6  (0x40)   global_kernel      Function is a __global__ kernel
  bit 7  (0x80)   global_confirmed   Always set by __global__ handler tail guard
```

### Combined Patterns

The attribute handlers do not set individual bits. They OR entire patterns into the byte. Each CUDA keyword produces a fixed bitmask:

| Keyword | OR mask(s) | Result byte | Handler | Evidence |
|---|---|---|---|---|
| `__global__` | `0x61` then `0x80` | `0xE1` | `sub_40E1F0` (`apply_nv_global_attr`) | `entity+182 |= 0x61; ... entity+182 |= 0x80;` |
| `__device__` | `0x23` | `0x23` | `sub_40EB80` (`apply_nv_device_attr`) | `entity+182 |= 0x23` |
| `__host__` | `0x15` | `0x15` | `sub_4108E0` (`apply_nv_host_attr`) | `entity+182 |= 0x15` |
| `__host__ __device__` | `0x23` then `0x15` | `0x37` | Both handlers in sequence | OR of device + host masks |
| (no annotation) | none | `0x00` | -- | Implicit `__host__` |

The `0x80` bit is set unconditionally at the end of `apply_nv_global_attr`. After the main body ORs `0x61` into byte+182 (setting bit 6 = `global_kernel`), a tail guard checks bit 6 and always ORs `0x80`:

```c
// sub_40E1F0, lines 84-88
v10 = *(_BYTE *)(a2 + 182);
if ( (v10 & 0x40) == 0 )       // if bit 6 (global_kernel) not set, bail
    return a2;                  // (only reachable via early error paths)
*(_BYTE *)(a2 + 182) = v10 | 0x80;   // always set for __global__
```

Since `0x61` was already OR'd in, bit 6 is always set on the normal path, so `0x80` is always applied. The actual result byte for any successful `__global__` application is `0x61 | 0x80 = 0xE1`. The guard condition only triggers on error paths where `0x61` was never applied (e.g., the template-lambda error at line 21 which returns before reaching line 56).

### Extraction Patterns

Code throughout cudafe++ extracts execution space category using bitmask tests:

| Mask | Test | Meaning | Used in |
|---|---|---|---|
| `& 0x30` | `== 0x00` | No explicit annotation (implicit host) | Space classification |
| `& 0x30` | `== 0x10` | `__host__` only | Space classification |
| `& 0x30` | `== 0x20` | `__device__` only | `nv_is_device_only_routine` |
| `& 0x30` | `== 0x30` | `__host__ __device__` | Space classification |
| `& 0x60` | `== 0x20` | Device, not kernel | Device-only predicate |
| `& 0x60` | `== 0x60` | `__global__` kernel (implies device) | Kernel identification |
| `& 0x40` | `!= 0` | Is a `__global__` kernel | Stub generation gate |

## Variable Memory Space Bitfield (Byte +148)

For variable entities (kind 7), byte `+148` encodes the CUDA memory space:

```
Byte at entity+148:

  bit 0  (0x01)   __device__     Variable resides in device global memory
  bit 1  (0x02)   __shared__     Variable resides in shared memory
  bit 2  (0x04)   __constant__   Variable resides in constant memory
```

These bits are mutually exclusive in valid programs. The attribute handlers enforce this by checking for conflicting combinations:

```c
// From apply_nv_device_attr (sub_40EB80), variable path:
a2->byte_148 |= 0x01;                      // set __device__
int shared_or_constant = a2->byte_148 & 0x06;   // check __shared__ | __constant__
if (popcount(shared_or_constant) + (a2->byte_148 & 0x01) == 2)
    error(3481, ...);                       // conflicting memory spaces
```

The `__device__` attribute on a function (kind 11) does NOT touch byte `+148`. It writes to byte `+182` (execution space) instead. The memory space byte is strictly for variables.

### Extended Memory Space (Byte +149)

```
Byte at entity+149:

  bit 0  (0x01)   __managed__    Unified memory, accessible from both host and device
```

Set by `apply_nv_managed_attr` (`sub_40E0D0`). The handler also sets bit 0 of `+148` (`__device__`) because managed memory resides in device global memory. Additional validation:

- Error 3481: conflicting if `__shared__` or `__constant__` is already set
- Error 3482: cannot be thread-local (`byte +161` bit 7)
- Error 3485: cannot be a local variable (`byte +81` bit 2)
- Error 3577: incompatible with `__grid_constant__` parameter (`byte +164` bit 2)

## Constexpr and Kernel Flags (Byte +176, +179)

### Byte +176: Member Function Flags

```
Byte at entity+176:

  bit 7  (0x80)   static_member   Function is a static class member
```

Tested by `apply_nv_global_attr` to detect `static __global__` functions. The check is `(signed char)(a2->byte_176) < 0`, which is true when bit 7 is set. Combined with the local-function test (`byte +81` bit 2 clear), this triggers warning 3507.

### Byte +179: Constexpr / Kernel Property Flags

```
Byte at entity+179:

  bit 1  (0x02)   kernel_body        Function has a kernel body (used for stub generation)
  bit 2  (0x04)   (instantiation)    Instantiation-required status
  bit 4  (0x10)   constexpr          Function is constexpr
  bit 5  (0x20)   noinline           Function is noinline
```

The kernel_body flag at bit 1 (`0x02`) is the primary gate for device stub generation. The backend code generator (`gen_routine_decl` in `cp_gen_be.c`) checks:

```c
// From gen_routine_decl (sweep p1.04, line ~1430)
if ((*(_BYTE *)(v3 + 182) & 0x40) != 0       // is __global__ kernel
    && (*(_BYTE *)(v3 + 179) & 2) != 0)       // has kernel body
{
    // Emit __wrapper__device_stub_<name>(<params>) forwarding body
}
```

The constexpr flag at bit 4 (`0x10`) is tested during `__global__` attribute validation. When set, the void-return-type check AND the lambda check are both skipped:

```c
// From apply_nv_global_attr (sub_40E1F0), lines 39-50
if ( (*(_BYTE *)(a2 + 179) & 0x10) == 0 )   // NOT constexpr
{
    // Non-constexpr __global__: check return type and lambda
    if ( (*(_BYTE *)(a2 + 191) & 1) != 0 )
        error(3506, ...);                    // lambda __global__ not allowed
    else if ( !is_void_return_type(a2) )
        error(3505, ...);                    // must return void
}
// If constexpr (bit 4 set): skip both checks entirely
```

This is a separate check from the static-member test (byte +176 bit 7 with byte +81 bit 2), which appears earlier at line 28:

```c
if ( *(char *)(a2 + 176) < 0         // static member (bit 7 set)
    && (*(_BYTE *)(a2 + 81) & 4) == 0 )  // not local
    warning(3507, "__global__");          // static __global__ warning
```

## Operator Function Kind (Byte +166)

```
Byte at entity+166:

  Value 5:  operator function (operator(), operator+, etc.)
```

Tested during `__global__` attribute application. If the entity is an operator function (value == 5), error 3644 is emitted: `operator()` cannot be declared `__global__`.

```c
// From apply_nv_global_attr (sub_40E1F0), line 30-31
if ( *(_BYTE *)(a2 + 166) == 5 )
    sub_4F8200(7, 3644, a1 + 56);     // error: __global__ on operator function
```

This prevents declaring lambda call operators as kernels via the `__global__` attribute directly (extended lambdas use a different mechanism with wrapper types).

## Parameter List (Pointer +152)

For routine entities, offset `+152` holds a pointer to the function prototype structure. The prototype's first field (`+0`) points to the parameter list head -- a linked list of parameter entities.

The `__global__` attribute handler iterates this list to check two constraints:

1. **Variadic check**: prototype `+16` bit 0 indicates variadic parameters. If set, error 3503 is emitted (variadic `__global__` functions are not allowed).

2. **`__grid_constant__` check**: the post-validation function `nv_validate_cuda_attributes` (`sub_6BC890`) walks the parameter list looking for parameters with `byte +32` bit 1 set (the `__grid_constant__` flag on a parameter entity). If found on a non-`__global__` function, error 3702 is emitted.

```c
// From nv_validate_cuda_attributes (sub_6BC890), lines 26-39
// Walk parameter list from prototype
v10 = **(__int64 ****)(v2 + 152);    // parameter list head
while (v10) {
    if (((_BYTE)v10[4] & 2) != 0)    // parameter byte+32 bit 1 = __grid_constant__
        error(3702, ...);            // grid_constant on non-kernel parameter
    v10 = (__int64 **)*v10;          // next parameter
}
```

## CUDA Extended Flags (Byte +183)

```
Byte at entity+183:

  bit 3  (0x08)   __nv_register_params__   Function uses register parameter passing
  bit 6  (0x40)   __cluster_dims__ intent  cluster_dims attribute with no arguments
```

### __nv_register_params__ (Bit 0x08)

Set by `apply_nv_register_params_attr` (`sub_40B0A0`). When present, the post-validation function `nv_validate_cuda_attributes` checks whether the function is `__global__` or `__host__`, and emits error 3661 if so. Device-only functions (`__device__` without `__host__`) are exempt:

```c
// From nv_validate_cuda_attributes (sub_6BC890), lines 42-69
if ( (*(_BYTE *)(a1 + 183) & 8) == 0 )     // no __nv_register_params__
    goto check_launch_config;

if ( (v3 & 0x40) != 0 ) {                  // __global__ kernel
    v4 = "__global__";
    error(3661, &qword_126EDE8, v4);       // incompatible
} else if ( (v3 & 0x30) != 0x20 ) {        // NOT device-only (has host component)
    v4 = "__host__";
    error(3661, &qword_126EDE8, v4);       // incompatible
}
// else: device-only function -- __nv_register_params__ is allowed
```

The key check is `(v3 & 0x30) != 0x20`: when the execution space annotation bits indicate device-only (bits 4,5 = 0x20), the error is skipped. This means `__nv_register_params__` is valid only on `__device__` functions -- it is rejected on `__global__`, `__host__`, and `__host__ __device__` functions.

### __cluster_dims__ Intent (Bit 0x40)

Set by `apply_nv_cluster_dims_attr` (`sub_4115F0`) when the attribute is applied with zero arguments. This marks the function as "wants cluster dimensions" without specifying concrete values -- the values may come from a separate `__block_size__` attribute or from a template parameter.

## Template / Linkage Flags (Pointer +184)

Offset `+184` is a 48-bit (6-byte) field encoding template instantiation and linkage information. The `__global__` attribute handler tests a specific bit pattern to detect constexpr lambdas with template linkage:

```c
// From apply_nv_global_attr (sub_40E1F0), line 21
if ( (*(_QWORD *)(a2 + 184) & 0x800001000000LL) == 0x800000000000LL )
{
    // This is a template lambda with external linkage but no definition yet.
    // Applying __global__ to it is an error.
    v14 = sub_6BC6B0(a2, 0);    // get entity name
    sub_4F7510(3469, a1 + 56, "__global__", v14);
    return;
}
```

The mask `0x800001000000` tests two bits:
- Bit 47 (`0x800000000000`): template instantiation pending
- Bit 24 (`0x000001000000`): has definition body

When bit 47 is set but bit 24 is clear, the entity is a template lambda awaiting instantiation that has no body yet -- applying `__global__` (or `__device__`) to such an entity produces error 3469.

## Launch Configuration Struct (Pointer +256)

Offset `+256` holds a pointer to a lazily-allocated 56-byte launch configuration structure. This pointer is `NULL` for functions without any launch configuration attributes. The allocation function `sub_5E52F0` creates and zero-initializes the struct on first use.

### Launch Config Layout

```
struct launch_config_t {           // 56 bytes, allocated by sub_5E52F0
    int64_t  maxThreadsPerBlock;   // +0   from __launch_bounds__(arg1)
    int64_t  minBlocksPerMP;       // +8   from __launch_bounds__(arg2)
    int32_t  maxBlocksPerCluster;  // +16  from __launch_bounds__(arg3)
    int32_t  cluster_dim_x;        // +20  from __cluster_dims__(x) or __block_size__(x,y,z,cx)
    int32_t  cluster_dim_y;        // +24  from __cluster_dims__(y) or __block_size__(x,y,z,cx,cy)
    int32_t  cluster_dim_z;        // +28  from __cluster_dims__(z) or __block_size__(x,y,z,cx,cy,cz)
    int32_t  maxnreg;              // +32  from __maxnreg__(N)
    int32_t  local_maxnreg;        // +36  from __local_maxnreg__(N)
    int32_t  block_size_x;         // +40  from __block_size__(x)
    int32_t  block_size_y;         // +44  from __block_size__(y)
    int32_t  block_size_z;         // +48  from __block_size__(z)
    uint8_t  flags;                // +52  bit 0=cluster_dims_set, bit 1=block_size_set
};                                 //      3 bytes padding to 56
```

### Attribute-to-Field Mapping

| Attribute | Arguments | Fields Written | Handler |
|---|---|---|---|
| `__launch_bounds__(M)` | 1 int | `+0` = M | `sub_411C80` |
| `__launch_bounds__(M,N)` | 2 ints | `+0` = M, `+8` = N | `sub_411C80` |
| `__launch_bounds__(M,N,C)` | 3 ints | `+0` = M, `+8` = N, `+16` = C | `sub_411C80` |
| `__cluster_dims__(x)` | 1 int | `+20` = x, `+24` = 1, `+28` = 1, `+52` bit 0 | `sub_4115F0` |
| `__cluster_dims__(x,y)` | 2 ints | `+20` = x, `+24` = y, `+28` = 1, `+52` bit 0 | `sub_4115F0` |
| `__cluster_dims__(x,y,z)` | 3 ints | `+20` = x, `+24` = y, `+28` = z, `+52` bit 0 | `sub_4115F0` |
| `__cluster_dims__()` | 0 args | `entity+183` bit 6 (intent flag only) | `sub_4115F0` |
| `__maxnreg__(N)` | 1 int | `+32` = N | `sub_410F70` |
| `__local_maxnreg__(N)` | 1 int | `+36` = N | `sub_411090` |
| `__block_size__(x,y,z)` | 3 ints | `+40` = x, `+44` = y, `+48` = z, `+52` bit 1 | `sub_4109E0` |
| `__block_size__(x,y,z,cx,cy,cz)` | 6 ints | block + cluster dims, `+52` bits 0+1 | `sub_4109E0` |

### Post-Validation Constraints

The function `nv_validate_cuda_attributes` (`sub_6BC890`) performs cross-attribute validation after all attributes have been applied. The key checks on the launch config struct:

**1. `__launch_bounds__` only on `__global__`:**
```c
// sub_6BC890, lines 45-51
v5 = *(_QWORD *)(a1 + 256);         // launch config pointer
if ( !v5 )  goto done;
if ( (v3 & 0x40) != 0 )             // if __global__, skip to next check
    goto check_cluster;
// Not __global__ but has launch_bounds values
if ( *(_QWORD *)v5 || *(_QWORD *)(v5 + 8) )
    error(3534, "__launch_bounds__");   // launch_bounds on non-kernel
```

**2. `__cluster_dims__`/`__block_size__` only on `__global__`:**
```c
// sub_6BC890, lines 81-87
if ( (*(_BYTE *)(a1 + 183) & 0x40) != 0    // cluster_dims intent
    || *(int *)(v5 + 20) >= 0 )             // cluster_dim_x set
{
    v11 = "__cluster_dims__";
    if ( *(int *)(v5 + 40) > 0 )
        v11 = "__block_size__";
    error(3534, v11);                       // not allowed on non-kernel
}
```

**3. `maxBlocksPerCluster` vs cluster product:**
```c
// sub_6BC890, lines 101-114
v6 = *(int *)(v5 + 20);                    // cluster_dim_x
if ( (int)v6 > 0 ) {
    v7 = *(int *)(v5 + 16);                // maxBlocksPerCluster
    if ( (int)v7 > 0
        && v7 < *(int*)(v5 + 28) * *(int*)(v5 + 24) * v6 )
    {
        // maxBlocksPerCluster < cluster_dim_x * cluster_dim_y * cluster_dim_z
        error(3707, "__cluster_dims__");    // inconsistent values
    }
}
```

**4. `__maxnreg__` only on `__global__`:**
```c
// sub_6BC890, lines 116-121
if ( *(int *)(v5 + 32) < 0 )               // maxnreg not set (sentinel -1)
    goto check_launch_maxnreg_conflict;
if ( (v9 & 0x40) == 0 )                    // not __global__
    error(3715, "__maxnreg__");             // maxnreg on non-kernel
```

**5. `__launch_bounds__` + `__maxnreg__` conflict:**
```c
// sub_6BC890, lines 144-145
if ( *(_QWORD *)v5 )                       // maxThreadsPerBlock set
    error(3719, "__launch_bounds__ and __maxnreg__");
```

## Entity Kind Reference

The entity kind byte at `+80` determines which offsets are valid. CUDA attribute handlers gate on this value:

| Kind | Value | CUDA offsets used | Handler examples |
|---|---|---|---|
| Variable | 7 | `+148`, `+149`, `+161`, `+164` | `__device__`, `__shared__`, `__constant__`, `__managed__` |
| Field | 8 | `+136` | `packed`, `aligned` (non-CUDA) |
| Routine | 11 | `+144`, `+152`, `+166`, `+176`, `+179`, `+182`, `+183`, `+184`, `+191`, `+256` | All execution space attrs, launch config |

## Cross-References

- [Execution Spaces](../cuda/execution-spaces.md) -- deep dive on byte `+182` semantics and the six virtual override mismatch errors
- [Attributes Overview](../attributes/overview.md) -- attribute kind enum (86-108) and `apply_one_attribute` dispatch
- [IL Overview](../il/overview.md) -- IL entry kinds 7 (variable), 8 (field), 11 (routine) node sizes
- [Scope Entry](scope-entry.md) -- 784-byte scope structure that contains entity chains
