# PTX Directive Handling

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

PTX directives — `.version`, `.target`, `.entry`, `.func`, `.global`, `.shared`, `.local`, `.const`, `.reg`, `.param`, `.weak`, `.common`, `.extern`, `.visible`, `.alias`, `.pragma` — are parsed and semantically validated by the Bison reduction actions embedded in the 48 KB parser function `sub_4CE6B0`. Unlike instructions, which pass through opcode table lookup (`sub_46E000`) and per-instruction semantic validators, directives are handled entirely within the Bison reduction switch:

- Each grammar production's action block reads values from the parser value stack.
- It validates them against the current PTX version and target architecture.
- It writes the results into the 1,200-byte parser state object or its child compile-unit state (`CU_state`).

> No intermediate AST is constructed; directives take effect immediately during parsing.

The state object carries the per-state-space symbol bookkeeping and the version gates:

- **18 linked lists** (9 head/tail pairs at offsets 368–512) track symbols per state space.
- A **string-keyed hash map** (offset 208) holds target feature flags.
- A **scope chain** (offset 984), rooted at offset 968, tracks nested function declarations.
- Two version-gating functions guard every directive introduced after the baseline ISA:
  - `sub_489050` — PTX ISA version.
  - `sub_489390` — SM architecture.

| | |
|---|---|
| **Bison parser** | `sub_4CE6B0` (48,263 bytes, 631 case labels) |
| **Version validator** | `sub_44A100` (bsearch over 44 valid PTX version IDs at `xmmword_1CFD940`) |
| **PTX version gate** | `sub_489050` — `sub_454E70` + `sub_455A80(major, minor, state)` |
| **SM arch gate** | `sub_489390` — checks `state+168 >= required_sm` |
| **Target handler** | `sub_4B1080` (per-target, texmode logic) |
| **Function handler** | `sub_497C00` (entry/func declarations, ABI) |
| **Variable handler** | `sub_4A0CD0` (state-space declarations, type validation) |
| **Parameter allocator** | `sub_44F6E0` (48-byte parameter nodes) |
| **Scope manager** | `sub_44B9C0` (scope hash map at `state+1008`) |
| **State-space lists** | 18 linked lists at `state+368`--`state+512` |
| **Target feature map** | Hash map at `state+208` (string keys, presence values) |

## Architecture

```text
PTX source text
     |
     v
+-------------------------------------------------------------------+
| BISON LALR(1) PARSER  sub_4CE6B0                                  |
| 631 reduction cases, each a direct action                         |
|                                                                   |
|   DIRECTIVE       CASES           HANDLER                         |
|   .version        35              sscanf + sub_44A100             |
|   .target         5, 38           sub_4B1080 (per-target)         |
|   .address_size   10              inline validation                |
|   .entry          82, 86-88       sub_497C00                      |
|   .func           97, 100-105     sub_497C00                      |
|   .global/shared  57-68           sub_4A0CD0                      |
|     /local/const                                                  |
|   .reg/.param     110-112         inline + sub_48BE80             |
|   .weak           55              sub_489050(3,1)                 |
|   .common         56              sub_489050(5,0)                 |
|   .extern         79              sets CU+81, linkage=3           |
|   .visible        80              sets CU+81, linkage=2           |
|   .alias          41              sub_4036D9 (param match)        |
|   .pragma         42              prefix-match dispatch chain     |
|                                                                   |
+-------------------+-----------------------------------------------+
                    |
          +---------+---------+
          v                   v
  PARSER STATE OBJECT     CU_STATE (compile-unit)
  ~1200 bytes             pointed to by state+1096
  state+144: version      CU+0:   linkage code
  state+152: target       CU+24:  state-space ID
  state+160: ptx_major    CU+48:  func metadata buf
  state+164: ptx_minor    CU+80:  return type
  state+168: sm_id        CU+81:  declaration linkage
  state+196: addr_size    CU+88:  current function
  state+208: feature map  CU+156: noinline pragma
  state+368: 18 ll heads  CU+172: reg-usage pragma
  state+968: scope root   CU+784: arch capability
  state+984: scope chain  CU+2448: target string
  state+1008: scope map   CU+2456: version string
```

## `.version X.Y` — Case 35

The `.version` directive establishes the PTX ISA version for the compilation unit. The parser extracts the major and minor version integers from the grammar, validates the combined version against a sorted table of 44 known versions, and stores both the numeric and string forms.

```c
// Reconstructed from case 35 of sub_4CE6B0
int major = sub_449950();       // extract major from parser state
int minor = sub_449960();       // extract minor from parser state
sscanf(token, "%d.%d", &major, &minor);

// Allocate formatted version string
char* ver_str = pool_alloc(pool, 5);
sprintf(ver_str, "%d.%d", major, minor);

// Validate: bsearch over 44 valid version IDs
int combined = major * 10 + minor;
if (!sub_44A100(combined))
    fatal_error("Unsupported PTX version %s", ver_str);

pool_free(ver_str);

// Store in parser state
state->version_string = token;          // state+144
state->ptx_major = major;               // state+160
state->ptx_minor = minor;               // state+164
CU_state->version_string = token;       // CU+2456
```

### Version Validation — `sub_44A100`

```c
// sub_44A100: validate PTX version against known versions
bool sub_44A100(int version_id) {
    int key = version_id;
    return bsearch(&key,
                   xmmword_1CFD940,   // sorted table base
                   0x2C,              // 44 entries
                   4,                 // sizeof(int)
                   compar) != NULL;   // simple integer compare
}
```

The 44-entry table at `xmmword_1CFD940` contains the combined version IDs (`major*10 + minor`) for every PTX ISA version recognized by ptxas v13.0. This covers PTX 1.0 through 8.7+.

## `.target sm_XX` — Cases 5 and 38

The `.target` directive accepts a comma-separated list of targets: SM architecture identifiers (`sm_XX`, `compute_XX`) and feature modifiers (`texmode_unified`, `texmode_independent`, `texmode_raw`, `map_f64_to_f32`, `debug`).

### Case 38 — Target List Iteration

```c
// Reconstructed from case 38
for (node = list_begin(*v5); !list_end(node); node = list_next(node)) {
    char* target_str = list_value(node);
    sub_4B1080(target_str, location, state);
}
```

### Per-Target Handler — `sub_4B1080`

The function branches on whether the target string contains `"sm_"` or `"compute_"`.

**SM/compute targets:**

```c
// SM target path in sub_4B1080
state->target_string = target_str;       // state+152
CU->target_string = target_str;          // CU+2448
state->arch_variant = sub_1CBEFD0(target_str);  // state+177

int sm_id;
sscanf(target_str + prefix_len, "%d", &sm_id);
state->target_id = sm_id;               // state+168
if (sm_id > state->max_target)
    state->max_target = sm_id;           // state+204

// Validate against one of three target tables:
//   compute_ targets:  unk_1D16160 (6 entries, 12 bytes each)
//   sm_ sub-variant:   unk_1D161C0 (7 entries, 12 bytes each)
//   standard sm_:      unk_1D16220 (32 entries, 12 bytes each)
// Each entry: { sm_id, required_ptx_major, required_ptx_minor }
entry = bsearch(&sm_id, table, count, 12, sub_484B70);
if (entry) {
    if (!sub_455A80(entry->ptx_major, entry->ptx_minor, state))
        state->version_mismatch_flag |= 1;   // state+178
}
```

**Feature modifiers:**

| Modifier | PTX Requirement | Action | CU State |
|---|---|---|---|
| `map_f64_to_f32` | Deprecated for sm > 12 | Stored in feature map; `CU+152 \|= 1` | Feature flag |
| `texmode_unified` | — | Stored in feature map; default if none specified | Default |
| `texmode_independent` | PTX >= 1.5 | Stored in feature map; `CU+2464 = 1` | Tex mode |
| `texmode_raw` | Requires `state+220` flag | Stored in feature map; `CU+2465 = 1` | Tex mode |
| `debug` | PTX >= 3.0 | `CU+2466 = 1`; `state+1033 = 1`; `state+834 = 1` | Debug on |

Texmode values are **mutually exclusive**. Each setter checks the feature hash map at `state+208` for conflicting entries before inserting:

```c
// texmode_unified path in sub_4B1080
if (map_get(state->feature_map, "texmode_independent"))
    error("conflicting texmode: %s", target_str);
if (map_get(state->feature_map, "texmode_raw"))
    error("conflicting texmode: %s", target_str);
map_put(state->feature_map, "texmode_unified", 1);
```

### Case 5 — Automatic Texmode Inference

When the `.target` directive omits an explicit texmode, case 5 infers one based on CLI flags:

```c
if (arch_supports_texmode(CU->arch_capability)) {
    if (!map_has(feature_map, "texmode_independent") &&
        !map_has(feature_map, "texmode_raw")) {
        if (state->cli_texmode_independent)
            sub_4B1080("texmode_independent", loc, state);
        else if (state->cli_texmode_raw)
            sub_4B1080("texmode_raw", loc, state);
        else
            sub_4B1080("texmode_unified", loc, state);
    }
}
```

## `.address_size 32|64` — Case 10

```c
// Reconstructed from case 10
sub_489050(state, 2, 3, ".address_size directive", location);  // PTX >= 2.3

int value = stack_value;
if (((value - 32) & ~0x20) != 0)       // allows exactly 32 and 64
    error("Invalid address size: %d", value);

state->address_size = value;             // state+196
```

The bit trick `(v - 32) & ~0x20` passes for exactly two values:
- `v=32`: `(0) & 0xFFFFFFDF = 0`
- `v=64`: `(32) & 0xFFFFFFDF = 0`

Any other value produces a nonzero result and triggers an error.

## `.entry` / `.func` Declarations — Cases 76+, 82, 88, 97, 103

Function and entry declarations span multiple Bison productions because the grammar decomposes them into prototype, parameter list, linkage qualifier, and body productions. The central handler `sub_497C00` processes both entry functions and device functions.

### `sub_497C00` — Function Declaration Handler

```c
// Reconstructed signature
int64 sub_497C00(
    state,          // parser state
    int decl_type,  // 1=visible, 2=forward, 3=extern, 4=static, 5=definition
    name,           // function name token
    return_params,  // return parameter list (NULL for entries)
    params,         // input parameter list
    bool is_entry,  // 1 for .entry, 0 for .func
    bool is_func,   // CU+80 qualifier for .func
    scratch_regs,   // scratch register list
    int retaddr,    // return address allocno (-1 if none)
    bool noreturn,  // .noreturn attribute
    bool unique,    // .unique attribute
    bool force_inline, // .FORCE_INLINE attribute
    location        // source location token
);
```

**Processing steps:**

1. **Scope creation**: `sub_44B9C0(state)` creates a new scope context. The scope hash map at `state+1008` maps scope IDs (starting at 61) to 40-byte scope descriptors.

2. **Parameter node allocation**: `sub_44F6E0(state, scope, name, 0, 0, location)` allocates a 48-byte parameter descriptor: `{type_info, name, scope, alignment, init_data, location}`.

3. **Symbol lookup**: `sub_4504D0(state+968, name, 1, state)` searches the current scope chain for an existing declaration.

4. **Forward declaration resolution**: If a matching forward declaration exists, the handler validates compatibility:
   - Declaration type consistency (except `2->1` and `4->1` promotions)
   - Parameter list type/alignment/state-space matching via `sub_484DA0`
   - Return parameter matching via `sub_484DA0`
   - Scratch register count and types
   - Return address register, first parameter register
   - `.noreturn` and `.unique` attribute consistency
   - Unified identifier matching

5. **New function creation**: If no prior declaration:
   - Registers in `state+968` (regular scope) or `state+976` (extern scope)
   - Calls `sub_44FDC0` to record ABI metadata
   - For Blackwell GB10B architecture (`sub_70FA00(CU, 33)`): allocates `__nv_reservedSMEM_gb10b_war_var` in shared memory as a hardware workaround

### Case 82 — Entry Function

```c
// Case 82: .entry declaration
if (CU->output_param_context)
    error("Parameter to entry function");

result = sub_497C00(state, decl_type, name,
                    NULL,    // no return params for entries
                    params,
                    1,       // is_entry = true
                    0,       // is_func = false
                    NULL,    // no scratch regs
                    -1,      // no retaddr
                    0, 0, 0, // no .noreturn/.unique/.force_inline
                    location);
```

### Case 88 — Entry Function Body Completion

After the function body is parsed, case 88 performs the final validation pass:

1. **Performance directive validation**:
   - `.maxntid` and `.reqntid` are mutually exclusive
   - `.maxnctapersm`/`.minnctapersm` require either `.maxntid` or `.reqntid`
   - `.reqntid` + `.reqnctapercluster` require `.blocksareclusters`
   - `.reqnctapercluster` and `.maxclusterrank` are mutually exclusive

2. **Kernel parameter size limits** (computed via `sub_42CBF0` + `sub_484ED0`):

   | PTX Version | Max Kernel Param Size |
   |---|---|
   | < 1.5 | 256 bytes |
   | >= 1.5, < 8.1 | 4,352 bytes |
   | >= 8.1 | 32,764 bytes |

   Parameters exceeding 4,352 bytes also require SM >= 70 and PTX >= 8.1.

3. **Debug labels**: Generates `__$startLabel$__<name>` and `__$endLabel$__<name>` for DWARF debug info.

4. **Debug hash**: If debug mode enabled (`state+856 != 0`), computes `CRC32(name) % 0xFFFF + base` as a debug identifier stored at `func->80+176`.

### Case 97 — Device Function

```c
// Case 97: .func declaration
result = sub_497C00(state, decl_type, name,
                    return_params, params,
                    0,                   // is_entry = false
                    CU->return_qualifier, // CU+80
                    scratch_regs, retaddr,
                    noreturn, unique, force_inline,
                    location);
```

## State-Space Declarations — `.global`, `.shared`, `.local`, `.const`

State-space directives set the "current state space" field (`CU+24`) and then delegate to `sub_4A0CD0` for variable declaration processing or `sub_4A2020` for declaration-without-initializer processing.

### State-Space Code Assignment

| Case | Action | State Space |
|---|---|---|
| 57 | `*CU = 1` | (extern/unresolved) |
| 59 | `*CU = 3` | `.shared` |
| 61 | `*CU = 2` | `.global` |
| 63 | `*CU = 4` | `.local` |
| 65 | `*CU = 5` | `.const` |
| 67 | `*CU = 0` | `.reg` |
| 58, 60, 62, 64, 66, 68 | `sub_4A2020(...)` | Process declaration in current space |

The odd-numbered cases set the state-space code; the immediately following even-numbered cases trigger the actual declaration processing.

### Variable Validator — `sub_4A0CD0`

This 4,937-byte function validates variable declarations across all state spaces. Key checks:

1. **Type validation**: Resolves `.texref` via `sub_450D00`. For types 9 (`.surfref`) and 10 (`.texref`), enforces `.tex` deprecation after PTX 1.5 and `.surfref` scope restrictions.

2. **`.b128` type**: Requires PTX >= 8.3 (`sub_455A80(8, 3)`) and SM >= 70 (`sub_489390(state, 70)`).

3. **State-space restrictions**:
   - `.managed` valid only with `.global` (space 5)
   - `.reserved` valid only with `.shared` (space 8)
   - `.reserved` shared alignment must be <= 64
   - `.common` valid only with `.global`
   - `.param` at file scope requires `.const` space
   - `.local` const disallowed at file scope

4. **Texmode interaction**:
   - `.surfref` types require `texmode_independent` in the feature map
   - `.tex`/`.texref` types incompatible with `texmode_raw`

5. **Initializer handling**: If an initializer is present, calls `sub_4A02A0` to validate constant expressions (no function pointers, no entry functions as values, no opaque type initializers).

### State-Space Linked Lists — 18 Lists at `state+368`

The parser maintains 18 linked list heads (9 head/tail pairs) at state offsets 368–512 to track declared symbols per state space:

```text
Offset    Pair   State Space
368/376   0      .global
384/392   1      .shared
400/408   2      .local
416/424   3      .const
432/440   4      .param
448/456   5      .tex
464/472   6      .surf
480/488   7      .sampler
496/504   8      reserved / other
```

**Initialization** (case 3 — section begin): Iterates `j` from 0 to 144 in steps of 8, allocating an 88-byte sentinel node (type=6) for each list. Each node's `+48` field links to per-section tracking data at `state+656 + j`.

**Scope teardown** (case 76 — new compilation unit): Destroys old symbol tables via `sub_425D20`, clears the target feature map, and merges scope-level lists into the parent scope by concatenating linked list chains for offsets 16, 48, 112, 128, 144, and 184 of the scope node.

## `.reg` / `.param` — Register and Parameter Declarations

Within function bodies, `.reg` and `.param` declarations create typed register/parameter entries. Three grammar productions handle the variants:

### Declaration Node Layout (56 bytes)

| Offset | Type | Field |
|---|---|---|
| 0 | ptr | Type list pointer |
| 8 | ptr | Name pointer |
| 16 | int32 | State-space code |
| 20 | byte | Is array |
| 21 | byte | Is vector |
| 24 | int32 | Alignment |
| 28 | byte | Extra flags |
| 40 | int32 | Count / range start |
| 44 | int32 | Range end (`0xFFFFFFFF` = no upper bound) |
| 48 | ptr | Auxiliary data |

**Case 110** — Single declaration: Reads type info from `CU_state` (offsets +16, +24, +28, +29, +32, +36), allocates the 56-byte node, sets count from the parsed integer, and calls `sub_48BE80(state)` to validate.

**Case 111** — Range declaration: Same as 110 but sets both start and end bounds. The sentinel value `0xFFFFFFFF` at offset 44 distinguishes range from single declarations.

**Case 112** — Vector declaration: Handles vector type qualifiers (`.v2`, `.v4`).

## Visibility / Linkage Directives

### `.weak` — Case 55

```c
sub_489050(state, 3, 1, ".weak directive", location);  // PTX >= 3.1
```

### `.common` — Case 56

```c
sub_489050(state, 5, 0, ".common directive", location);  // PTX >= 5.0
```

### Linkage Qualifiers — Cases 78–81

These set `CU+81` (declaration linkage type) within function prototype production contexts:

| Case | Linkage | PTX Directive |
|---|---|---|
| 78 | 1 | *(no qualifier)* — default internal/static linkage |
| 79 | 3 | `.extern` |
| 80 | 2 | `.visible` |
| 81 | 4 | `.weak` |

## `.alias` — Case 41

Symbol aliasing requires PTX >= 6.3 and SM >= 30:

```c
// Reconstructed from case 41
sub_489050(state, 6, 3, ".alias", location);   // PTX >= 6.3
sub_489390(state, 0x1E, ".alias", location);   // SM >= 30

sym1 = sub_4504D0(state->scope_chain, name1, 1, state);
sym2 = sub_4504D0(state->scope_chain, name2, 1, state);

if (!sym1) error("undefined symbol: %s", name1);
if (!sym2) error("undefined symbol: %s", name2);
```

**Validation:**
- Both symbols must be function type (node type == 5)
- `sym1` must not already have a body defined (`sym1->80->88 == 0`)
- Neither can be entry functions
- No self-aliasing (names must differ)
- Parameter lists must match (calls `sub_4036D9` twice: once for return params, once for input params)
- `.noreturn` attribute must be consistent across both symbols
- Cannot alias to `.extern` or declaration-qualified functions

On success: `sym1->80->64 = sym2` (sets the alias-target pointer).

## `.pragma` — Case 42

The `.pragma` directive requires PTX >= 2.0 and dispatches through a prefix-matching chain. Each pragma string is compared against known prefixes via `sub_4279D0` (starts-with test):

```c
// Reconstructed dispatch structure from case 42
for (node = list_begin(pragma_list); !list_end(node); node = list_next(node)) {
    char* pragma_str = list_value(node);
    sub_489050(state, 2, 0, ".pragma directive", location);  // PTX >= 2.0

    char* arch_str = sub_457CB0(CU->arch_descriptor, index);
    if (starts_with(arch_str, pragma_str)) {
        // matched known pragma
        dispatch_to_handler(pragma_str, state);
    }
}
```

### Pragma Dispatch Chain

| Priority | Prefix Index | Pragma | Handler | Storage |
|---|---|---|---|---|
| 1 | `sub_457CB0(arch, 1)` | `"noinline"` | `sub_456A50` + `sub_48D8F0` | `CU+156`, `CU+192` |
| 2 | `sub_457CB0(arch, 3)` | inline-related | Sets `CU+160 = 1` | `CU+160` |
| 3 | `sub_457CB0(arch, 16)` | register-usage | `sub_4563E0` + `sub_48C370` | `CU+172` |
| 4 | `sub_457CB0(arch, 5)` | min threads | `sub_4563E0` + `sub_48C6F0` | `CU+164` or `CU+168` |
| 5 | `sub_457CB0(arch, 9)` | max constraint | `sub_4567E0` + `sub_403D2F` | `CU+176` |
| 6 | `sub_457CB0(arch, 10)` | min constraint | `sub_4567E0` + `sub_403D2F` | `CU+184` |
| 7 | `sub_457CB0(arch, 18)` | deprecated | Warning via `dword_29FA6C0` | — |
| 8 | `sub_457CC0(arch, 1)` | deprecated | Warning via `dword_29FA6C0` | — |
| 9–11 | `sub_457C60/CA0/C70` | unsupported | Warning via `dword_29FA7F0` | — |
| 12 | `sub_457D30/D50` | unsupported | Warning via `dword_29FA7F0` | — |
| 13 | `sub_457CB0(arch, 22)` | function-level | Appends to func or module pragma list | `func->80->80` or `state+272` |

Unmatched pragmas trigger an error via `dword_29FA6C0`.

## Feature Version Gating

Two functions guard every directive against minimum PTX ISA version and SM architecture requirements. They are called hundreds of times throughout the Bison reduction actions.

### `sub_489050` — PTX ISA Version Check

```c
// sub_489050(state, required_major, required_minor, directive_name, location)
char sub_489050(state, int major, int minor, char* name, location) {
    if (sub_454E70(state->version_check_disabled))  // state+960
        return 1;  // checks disabled
    if (state->lenient_mode)  // state+832
        return 1;  // lenient mode
    if (!sub_455A80(major, minor, state)) {
        char buf[152];
        sprintf(buf, "%d.%d", major, minor);
        sub_42FBA0(error_desc, location, name, buf);
    }
    return result;
}
```

### `sub_489390` — SM Architecture Check

```c
// sub_489390(state, required_sm, directive_name, location)
char sub_489390(state, uint required_sm, char* name, location) {
    if (sub_454E70(state->version_check_disabled))  // state+960
        return 1;
    if (!state->target_string || state->target_id > required_sm) {
        // state+152 == NULL or state+168 > required_sm
        char buf[48];
        sprintf(buf, "sm_%d", required_sm);
        sub_42FBA0(error_desc, location, name, buf);
    }
    return result;
}
```

### Version Requirements by Directive

| Directive | PTX ISA | SM Architecture |
|---|---|---|
| `.address_size` | >= 2.3 | — |
| `.weak` | >= 3.1 | — |
| `.common` | >= 5.0 | — |
| `.alias` | >= 6.3 | >= 30 |
| `.branchtargets` | >= 6.0 | >= 30 |
| `.calltargets` | >= 2.1 | >= 20 |
| `.callprototype` | >= 2.1 | >= 20 |
| `.pragma` | >= 2.0 | — |
| `texmode_independent` | >= 1.5 | — |
| `debug` target | >= 3.0 | — |
| kernel param list | >= 1.4 | — |
| opaque types | >= 1.5 | — |
| `.b128` type | >= 8.3 | >= 70 |
| kernel params > 4352B | >= 8.1 | >= 70 |

## Parser State Object Layout

The parser state object (`v1127` / `a1` in `sub_4CE6B0`) is approximately 1,200 bytes. Key offsets for directive handling:

| Offset | Type | Field |
|---|---|---|
| 72 | ptr | Module-level output buffer |
| 88 | ptr | Current function link |
| 144 | char* | `.version` string (e.g., `"8.5"`) |
| 152 | char* | `.target` string (e.g., `"sm_90"`) |
| 160 | int32 | PTX major version |
| 164 | int32 | PTX minor version |
| 168 | int32 | SM architecture ID |
| 177 | byte | Architecture sub-variant flag |
| 178 | byte | Version mismatch flag |
| 196 | int32 | `.address_size` (32 or 64) |
| 204 | int32 | Maximum SM ID encountered |
| 208 | ptr | Target feature hash map |
| 219 | byte | CLI `texmode_independent` flag |
| 220 | byte | CLI `texmode_raw` flag |
| 272 | ptr | Module pragma list head |
| 368–512 | ptr[18] | State-space linked list heads |
| 656–800 | bytes | Per-section tracking data (144 bytes) |
| 832 | byte | Lenient mode flag |
| 834 | word | Debug mode flags |
| 856 | int32 | Debug hash base |
| 960 | int32 | Version check disable flag |
| 968 | ptr | Scope root (top-level symbol table) |
| 976 | ptr | Extern function scope |
| 984 | ptr | Current scope chain pointer |
| 1000 | byte | Function body active flag |
| 1008 | ptr | Scope hash map |
| 1033 | byte | Debug info enabled |
| 1096 | ptr | CU_state pointer |

## Function Map

| Address | Size | Identity | Callers |
|---|---|---|---|
| `sub_44A100` | 39 B | PTX version bsearch validator | case 35 |
| `sub_44B9C0` | 171 B | Scope context creator | case 82, 97 via `sub_497C00` |
| `sub_44F6E0` | 135 B | Parameter node allocator (48 B nodes) | `sub_497C00` |
| `sub_489050` | 115 B | PTX ISA version gate | ~30 directive cases |
| `sub_489390` | 85 B | SM architecture version gate | ~15 directive cases |
| `sub_497C00` | 2,992 B | Function/entry declaration handler | cases 82, 97 |
| `sub_4A0CD0` | 4,937 B | Variable/symbol declaration validator | cases 58–68 |
| `sub_4A02A0` | 2,607 B | Initializer/constant expression validator | `sub_4A0CD0` |
| `sub_4B1080` | ~700 B | Per-target handler (SM + texmode) | cases 5, 38 |
| `sub_4036D9` | 437 B | Parameter list compatibility check | case 41 (`.alias`) |
| `sub_4CE6B0` | 48,263 B | Bison parser (all directive cases) | compilation driver |
