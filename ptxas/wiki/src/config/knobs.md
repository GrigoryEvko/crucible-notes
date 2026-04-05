# Knobs System

The knobs system is ptxas's internal configuration mechanism -- a separate layer beneath the public CLI flags that exposes 1,294 tuning parameters to NVIDIA developers. Every significant compiler heuristic (register allocation thresholds, scheduling priorities, pass enable/disable, peephole rules) has a corresponding knob. The system is shared with cicc via a common header (`generic_knobs_impl.h`) but ptxas instantiates it twice: once for the DAG scheduler pipeline (99 knobs) and once for the OCG (Optimizing Code Generator) backend (1,195 knobs). All knob names are stored ROT13-encoded in the binary, a lightweight obfuscation that prevents casual `strings` discovery while being trivially reversible.

The knobs infrastructure lives primarily in two address regions: `0x6F0000`--`0x6F8000` (DAG knob instantiation, shared with the Mercury SASS pipeline) and `0x797000`--`0x7A2000` (OCG knob instantiation, the larger set). Both regions are compiled from the same template in `generic_knobs_impl.h`.

| | |
|---|---|
| **Total knobs** | 1,294 (99 DAG + 1,195 OCG) |
| **Source header** | `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/common/utils/generic/impl/generic_knobs_impl.h` |
| **DAG GetKnobIndex** | `sub_6F0820` (2,782 bytes) |
| **OCG GetKnobIndex** | `sub_79B240` (518 bytes) |
| **ParseKnobValue** | `sub_6F7360` / `sub_79F540` (DAG: 18KB, OCG: 18KB) |
| **ReadKnobsFile** | `sub_79D070` (9,879 bytes) |
| **KnobsInit (master)** | `sub_79D990` (40,817 bytes) |
| **KnobInit (per-knob)** | `sub_7A0C10` (13,874 bytes) |
| **Knob descriptor** | 64 bytes per entry |
| **Knob runtime value** | 72 bytes per slot |
| **Name obfuscation** | ROT13 with case-insensitive comparison |
| **Setting mechanisms** | `-knob NAME=VALUE`, knobs file (`[knobs]` header), PTX `pragma`, env var |
| **Debug dump** | `DUMP_KNOBS_TO_FILE` environment variable |

## Architecture

```
                  ┌──────────────────────────────────────────┐
                  │            KnobsInit (sub_79D990)        │
                  │  Called once from global init sub_662920  │
                  └─────┬──────────┬──────────┬──────────────┘
                        │          │          │
              ┌─────────▼──┐  ┌───▼──────┐  ┌▼───────────────┐
              │ ReadKnobsFile│  │ -knob CLI│  │ PTX pragma     │
              │ sub_79D070   │  │ parsing  │  │ (unless        │
              │ [knobs] fmt  │  │          │  │ DisablePragma) │
              └─────────┬───┘  └───┬──────┘  └┬───────────────┘
                        │          │           │
                        ▼          ▼           ▼
              ┌─────────────────────────────────────────────┐
              │       ParseKnobsString (sub_79B530)         │
              │  Handles WHEN=, INJECTSTRING, ~-delimited   │
              └──────────────────┬──────────────────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   GetKnobIndex           │
                    │   sub_6F0820 (DAG)       │
                    │   sub_79B240 (OCG)       │
                    │   ROT13 decode + lookup  │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   ParseKnobValue         │
                    │   sub_6F7360 (DAG)       │
                    │   sub_79F540 (OCG)       │
                    │   Type-specific parsing  │
                    └────────────┬─────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │   Runtime knob array     │
                    │   72 bytes per slot      │
                    │   Accessed by index      │
                    └──────────────────────────┘
```

## ROT13 Name Obfuscation

Every knob name in the binary is stored as a ROT13-encoded string. The `GetKnobIndex` function decodes each character inline during comparison, without ever materializing the cleartext name in memory. The decode is combined with a case-insensitive `tolower()` comparison against the user-supplied query.

The inline ROT13 decode from `sub_6F0820`:

```c
// For each character in the stored ROT13 name:
char c = stored_name[i];
if ((unsigned char)((c & 0xDF) - 65) <= 12)
    c += 13;                   // A-M (or a-m) -> N-Z (or n-z)
else if ((unsigned char)((c & 0xDF) - 78) < 13)
    c -= 13;                   // N-Z (or n-z) -> A-M (or a-m)
// Then compare case-insensitively:
if (tolower(query_char) != tolower(c))
    goto mismatch;
```

The `& 0xDF` trick converts lowercase to uppercase before range-checking, so both `'a'-'m'` and `'A'-'M'` hit the first branch. Non-alphabetic characters pass through unchanged. This means knob names like `SchedNumBB_Limit` with underscores and digits are handled correctly -- only the alphabetic portion rotates.

To reverse-engineer knob names from the binary: extract the ROT13 strings from the knob definition table (64-byte stride at the table base pointer), apply ROT13, and you get the cleartext name.

## Knob Descriptor Layout

Each knob is described by a 64-byte entry in the knob definition table. The table is an array at `(knob_state + 16)` with count at `(knob_state + 24)`.

```
Offset  Size  Field
──────  ────  ─────────────────────────────────────
+0      8     name_ptr          Pointer to ROT13-encoded primary name
+8      8     name_len          Length of primary name
+16     1     type_tag          Knob type (OKT_* enum, 1-12)
+17     7     (padding)
+24     16    (reserved)
+40     8     alias_ptr         Pointer to ROT13-encoded alias name
+48     8     alias_len         Length of alias name
+56     8     (reserved)
──────  ────
        64    Total
```

Both primary and alias names are checked during lookup. A knob matches if either its primary name or alias decodes to the query string (case-insensitive). The alias mechanism allows backward-compatible renaming of knobs across toolkit versions.

## Knob Value Layout

Runtime knob values are stored in a flat array of 72-byte slots at `(knob_state + 72 * index)`. The slot layout depends on the type:

```
Offset  Size  Field
──────  ────  ─────────────────────────────────────
+0      1     type_tag          Runtime type (0=unset, 1-10)
+1      7     (padding)
+8      8     value / pointer   Primary value (int32, int64, float, double, or pointer)
+16     8     list_begin        For list types: first element pointer
+24     8     list_sentinel     For list types: sentinel node
+32     4     aux_value         Secondary value (e.g., int-range high bound)
+36     4     (padding)
+40     8     list_tail         For list types: last element pointer
+48     8     list_head         For list types: head pointer
+56     4     element_count     For list types: number of elements
+60     4     (padding)
+64     8     allocator         Arena allocator pointer for list/range types
──────  ────
        72    Total
```

The type tag at runtime differs from the definition-table type tag. The definition type drives parsing; the runtime type reflects what was actually stored:

| Runtime Type | Meaning | Payload |
|---|---|---|
| 0 | Unset / invalid | None |
| 1 | int32 | `*(int32*)(slot + 8)` |
| 2 | float | `*(float*)(slot + 8)` |
| 3 | double / int64 | `*(int64*)(slot + 8)` |
| 4 | boolean (true) | No payload; presence = true |
| 5 | string | `*(char**)(slot + 8)` |
| 6 | when-condition list | Doubly-linked list at `+16..+48`, count at `+56` |
| 7 | int32 with secondary | `*(int32*)(slot + 8)`, `*(int32*)(slot + 12)` |
| 8 | int-range | `*(int32*)(slot + 8)` = low, `*(int32*)(slot + 12)` = high |
| 9 | opcode-string-list | Doubly-linked list (same structure as type 6) |
| 10 | int-list (dynamic) | Growable array at `+16`, count at `+24` |

### Per-Type Slot Usage (confirmed from decompilation)

**Types 1, 2, 3, 4, 5, 7, 8** -- scalar types using only bytes +0 through +15:

```
Type 1 (int32):      +0 = 0x01, +8 = int32 value (4 bytes)
Type 2 (float):      +0 = 0x02, +8 = float value (4 bytes, upper 4 undefined)
Type 3 (double):     +0 = 0x03, +8 = double value (8 bytes)
Type 4 (boolean):    +0 = 0x04  (no payload -- presence = true)
Type 5 (string):     +0 = 0x05, +8 = char* pointer (8 bytes, NOT owned)
Type 7 (budget):     +0 = 0x07, +8 = int32 primary, +12 = int32 secondary
Type 8 (int-range):  +0 = 0x08, +8 = int32 low, +12 = int32 high
```

**Types 6 and 9** -- doubly-linked list types using the full 72 bytes:

```
+0:   byte   type tag (6 or 9)
+8:   ptr    next pointer (initially 0)
+16:  ptr    → slot+24 (sentinel backward link)
+24:  ptr    → slot+8 (sentinel forward link)
+32:  int64  (unused, set to 0)
+40:  ptr    tail of list
+48:  ptr    head of list
+56:  int32  element count (starts at 2 for sentinel nodes)
+64:  ptr    arena allocator (for node allocation)
```

Each list node is 24 bytes, allocated from the arena at +64:

```
Type 6 node: [next(8), prev(8), string_ptr(8)]
Type 9 node: [next(8), prev(8), opcode_id(4) | int_value(4)]
```

**Type 10** -- dynamic growable array:

```
+0:   byte   = 0x0A
+8:   ptr    arena allocator
+16:  ptr    array base (int32 elements, grown via sub_6EFD20)
+24:  int32  element count (initialized to 0xFFFFFFFF = -1; first insert sets to 0)
```

The array grows by calling `sub_6EFD20(slot+8, count+2)` before each insertion, which reallocates if capacity is exceeded. Elements are 4-byte `int32` values stored contiguously starting at the base pointer.

## Knob Type System

The definition-table type tag (at descriptor offset `+16`) determines how `ParseKnobValue` interprets the value string. There are 10 logical knob types with 1,294 total registrations:

| Type Tag | Name | Count | Parse Rule |
|---|---|---|---|
| 1 | `OKT_NONE` | 139 | Boolean flag -- presence = true, no value needed |
| 2 | `OKT_INT` | 616 | `strtol(value, NULL, 0)` -- accepts decimal, hex (`0x`), octal (`0`) |
| 3 | `OKT_BDGT` | 88 | Same as INT but stores with secondary field zeroed (budget type) |
| 4 | `OKT_IRNG` | 8 | `"lo..hi"` range -- two integers separated by `..` |
| 5 | `OKT_ILIST` | 3 | Comma-separated integers: `"1,2,3,4"` |
| 6 | `OKT_FLOAT` | 12 | `sscanf(value, "%f", &result)` |
| 7 | `OKT_DBL` | 100 | `sscanf(value, "%lf", &result)` |
| 8 | `OKT_STR` | 28 | Direct string assignment (pointer copy) |
| 9 | `OKT_WHEN` | 2 | When-condition string; parsed into linked list of condition nodes |
| 10 | `OKT_OPCODE_STR_LIST` | 4 | Opcode-name,integer pairs: `"FADD,3,FMUL,2"` |
| 11 | `OKT_STR` (variant) | — | Same as type 8 (alternate string slot) |
| 12 | `OKT_ILIST` (variant) | — | Int-list with pre-initialized allocator |

The INT type (616 knobs, 47.6%) dominates. These control thresholds, limits, and numeric heuristic parameters across the entire compiler. BDGT (budget) knobs (88) are semantically similar to INT but carry a secondary field used for budget-tracking in cost models. The 100 DBL knobs control floating-point heuristic weights (scheduling priorities, cost ratios, etc.).

### Definition-Type to Runtime-Type Mapping

The definition-table type tag drives parsing; `ParseKnobValue` writes a different runtime type tag into the 72-byte slot. The mapping is not 1:1 -- several definition types collapse into the same runtime type, and compound types undergo a pre-initialization phase before the main parse:

| Def Type | Definition Name | Runtime Type | Runtime Name | Pre-init? |
|---|---|---|---|---|
| 1 | `OKT_NONE` | 4 | boolean (true) | No |
| 2 | `OKT_INT` | 1 | int32 | No |
| 3 | `OKT_BDGT` | 7 | int32 + secondary | No |
| 4 | `OKT_IRNG` | 8 | int-range (low, high) | No |
| 5 | `OKT_ILIST` | 10 | int-list (dynamic array) | No |
| 6 | `OKT_FLOAT` | 2 | float (single precision) | No |
| 7 | `OKT_DBL` | 3 | double (8-byte) | No |
| 8 | `OKT_STR` | 5 | string (pointer) | No |
| 9 | `OKT_WHEN` | 6 | linked list (when-condition) | Yes |
| 10 | `OKT_OPCODE_STR_LIST` | 9 | linked list (opcode-string) | Yes |
| 11 | `OKT_STR` (variant) | 5 | string (pointer) | No |
| 12 | `OKT_ILIST` (variant) | 10 | int-list (dynamic array) | Yes |

Types 11 and 12 are aliases: type 11 shares the exact handler with type 8 (both produce runtime type 5), and type 12 shares parsing logic with type 5 but its pre-switch initializes the allocator from the knob state object instead of inline.

### ParseKnobValue Dispatch Algorithm

`ParseKnobValue` (`sub_79F540`, source lines 435--551 of `generic_knobs_impl.h`) implements a two-phase dispatch. The first switch pre-initializes compound types; the second switch parses the value string.

**Phase 1 -- Pre-initialization (compound types only):**

```c
// v15 = definition type tag at (knob_descriptor + 16)
// v14 = runtime slot at (knob_state[9] + 72 * index)
switch (v15) {
case 9:   // OKT_WHEN -> runtime type 6
    KnobValueReset(v14);
    v14[0] = 6;
    // Initialize doubly-linked list with two sentinel nodes:
    //   +8  = 0 (next), +16 -> +24, +24 -> +8 (circular sentinels)
    //   +40 = tail, +48 = head, +56 = count (starts at 2)
    //   +64 = allocator from knob_state[1]
    break;

case 10:  // OKT_OPCODE_STR_LIST -> runtime type 9
    KnobValueReset(v14);
    v14[0] = 9;
    // Same linked-list initialization as case 9
    break;

case 12:  // OKT_ILIST variant -> runtime type 10
    KnobValueReset(v14);
    v14[0] = 10;
    *(ptr*)(v14 + 16) = NULL;           // growable array base
    *(ptr*)(v14 + 8)  = allocator;      // from knob_state[1]
    *(int32*)(v14 + 24) = 0xFFFFFFFF;   // sentinel count (-1)
    break;
}
```

**Phase 2 -- Value parsing (all types):**

**Type 1 (`OKT_NONE`, boolean):** No value string needed. Stores runtime type 4 (boolean true). Presence alone indicates the knob is set.

**Type 2 (`OKT_INT`, integer):** Calls `sub_6F71D0(value, NULL)` -- a `strtol` wrapper with base 0, which auto-detects decimal, hex (`0x` prefix), and octal (`0` prefix). Stores runtime type 1, value at slot+8 as `int32`.

**Type 3 (`OKT_BDGT`, budget):** Same integer parsing as type 2. Stores runtime type 7 with the primary value at slot+8 and the secondary (budget counter) at slot+12 zeroed. Cost models decrement the secondary field as optimization budget is consumed.

**Type 4 (`OKT_IRNG`, integer range):** Parses `"low..high"` format with these edge cases:

```
"100..200"    -> low=100,  high=200        Standard range
"100.."       -> low=100,  high=0x7FFFFFFF  Open upper bound
"..200"       -> low=0x80000000, high=200   Open lower bound
".."          -> low=0x80000000, high=0x7FFFFFFF  Full range
"42"          -> low=42, high=42            Degenerate (single value)
""            -> error "Empty integer range value"
```

The `..` separator is detected by checking `*endptr == '.' && endptr[1] == '.'`. Default bounds are `INT_MIN` (`0x80000000`) and `INT_MAX` (`0x7FFFFFFF`). Stores runtime type 8 with low at slot+8, high at slot+12.

**Type 5 (`OKT_ILIST`, integer list):** Parses comma-separated integers. Validation requires each element to start with a digit or `-`. Uses a growable array (runtime type 10) at slot+16, grown via `sub_6EFD20(slot+8, count+2)` before each insertion. Elements are 4-byte `int32` values stored contiguously. Example: `"1,2,3,4"` produces a 4-element array.

**Type 6 (`OKT_FLOAT`, float):** Calls `sscanf(value, "%f", &result)`. Stores runtime type 2, value at slot+8 as a 4-byte IEEE 754 single. Returns error `"Invalid floating point value"` if `sscanf` does not return 1.

**Type 7 (`OKT_DBL`, double):** Calls `sscanf(value, "%lf", &result)`. Stores runtime type 3, value at slot+8 as an 8-byte IEEE 754 double. Returns error `"Invalid double value"` if `sscanf` does not return 1.

**Type 8/11 (`OKT_STR`, string):** Both handled identically. Stores runtime type 5 with a direct pointer copy: `*(char**)(slot+8) = value`. The string is NOT duplicated -- the pointer references the original buffer, so the caller must ensure the string's lifetime exceeds the knob's.

**Type 9 (`OKT_WHEN`, when-condition):** Pre-switch already initialized the linked list (runtime type 6). Allocates a 24-byte node via the allocator's vtable (`allocator_vtable[3](allocator, 24)`). Node layout: `[next_ptr(8), prev_ptr(8), string_ptr(8)]`. The condition string pointer is stored at node+16. Nodes are inserted at the tail of the doubly-linked list. Error if value is NULL; empty string is permitted.

**Type 10 (`OKT_OPCODE_STR_LIST`, value-pair list):** Pre-switch already initialized the linked list (runtime type 9). Parsing loop:

1. Call `vtable+40` to split the next comma-delimited token into opcode name and integer value strings
2. If opcode name is NULL: error `"Empty opcode string"` (line 520)
3. If integer value is NULL: error `"Empty integer value"` (line 522)
4. Parse integer via `strtol(nptr, 0, 10)` (base 10 only, unlike OKT_INT)
5. Resolve opcode name to internal ID via `vtable+56` (SASS opcode table lookup)
6. Allocate 24-byte node: `[next(8), prev(8), opcode_id(4) | int_value(4)]`
7. Insert into linked list; loop until input exhausted

Format: `"FADD,3,FMUL,2"` produces two nodes: (FADD_id, 3) and (FMUL_id, 2). The opcode resolution uses the same 11,240-byte opcode recognition table as the peephole optimizer.

**Type 12 (`OKT_ILIST` variant, opcode list):** Pre-switch already initialized the growable array (runtime type 10). Parsing loop:

1. Call `vtable+64` to extract the next comma-delimited opcode name
2. Resolve to internal ID via `vtable+56`
3. Grow array via `sub_6EFD20(slot+8, count+2)`
4. Store opcode ID as `int32` in the array

Format: `"FADD,FMUL,IADD3"` -- opcode names only, no integers. Each is resolved to its internal opcode ID.

**Default:** Error `"Invalid knob type"` (line 551).

### Parse Error Messages

`ParseKnobValue` (`sub_79F540` / `sub_6F7360`) produces these diagnostic strings on parse failure:

| Error String | Source Line | Def Type | Condition |
|---|---|---|---|
| `"Empty when-string"` | 435 | 9 | WHEN knob with NULL value |
| `"Empty integer range value"` | 445 | 4 | IRNG knob with NULL or empty value |
| `"Empty integer list value"` | 451 | 5 | ILIST knob with NULL or empty value |
| `"Integer list value is not an integer"` | 453 | 5 | First char not digit or `-` |
| `"End of integer range value is not ',' or null character"` | 457 | 5 | ILIST terminator not `,` or `\0` |
| `"Empty integer value"` | 470 | 2 | INT knob with NULL or empty value |
| `"Empty integer value"` | 478 | 3 | BDGT knob with NULL or empty value |
| `"Empty floating point value"` | 491 | 6 | FLOAT knob with NULL or empty value |
| `"Invalid floating point value"` | 496 | 6 | `sscanf` returns != 1 |
| `"Empty double value"` | 502 | 7 | DBL knob with NULL or empty value |
| `"Invalid double value"` | 506 | 7 | `sscanf` returns != 1 |
| `"Empty value pair list"` | 515 | 10 | OPCODE_STR_LIST with NULL value |
| `"Empty opcode string"` | 520 | 10 | Opcode name resolves to NULL |
| `"Empty integer value"` | 522 | 10 | Integer after opcode resolves to NULL |
| `"Empty opcode list"` | 536 | 12 | Opcode-list variant with NULL value |
| `"Invalid knob type"` | 551 | — | Unrecognized type tag in definition table |
| `"Invalid knob identifier"` | 395 | — | `GetKnobIndex` -- name not found |

All errors carry source attribution: `generic_knobs_impl.h` with a line number and function name (`"GetKnobIndex"`, `"ParseKnobValue"`, `"ReadKnobsFile"`). Error constructors: `sub_79CDB0` (simple format string) and `sub_79AED0` (format with knob name and value context).

## Setting Knobs

### Method 1: `-knob` CLI Flag

```
ptxas -knob SchedNumBB_Limit=100 -knob DisableCSE=1 input.ptx -o output.cubin
```

Multiple `-knob` flags accumulate. Each is parsed by `KnobsInit` (`sub_79D990`) during startup. The knob name is looked up via `GetKnobIndex`, then the value is parsed according to the knob's type.

### Method 2: Knobs File

A knobs file is a plain-text file with a required `[knobs]` section header:

```ini
; Comments or metadata can appear before the header.
; ReadKnobsFile ignores everything until [knobs] is found.
[knobs]
SchedNumBB_Limit=100
DisableCSE=1
RegAllocBudget=5000
; WHEN= syntax is also supported inside the file:
WHEN=SH=0xDEADBEEF;SchedNumBB_Limit=200
```

`ReadKnobsFile` (`sub_79D070`, source lines 1060--1090 of `generic_knobs_impl.h`) processes the file:

```
1. fopen(path, "r")                               line ~1060
2. fseek(file, 0, SEEK_END)                        line 1075
3. size = ftell(file)                               line 1075
4. fseek(file, 0, SEEK_SET)                         line 1075
5. buffer = allocator->vtable[2](allocator, size+1) (heap alloc)
6. bytes = fread(buffer, 1, size, file)             line 1070
7. buffer[bytes] = '\0'                             (null-terminate)
8. marker = strstr(buffer, "[knobs]")               line 1065
9. if (!marker) error "Knobs header not found"
10. content = marker + 7                            (skip "[knobs]")
11. vtable[4](result, knob_state, content, 0)       (parse callback)
12. fclose(file)                                    line 1085
```

Key implementation details:

- **Entire file read at once.** The file is `fseek`/`ftell`-measured, then `fread` into a single buffer of `size + 1` bytes. No line-by-line streaming.
- **`strstr`-based header detection.** The `[knobs]` marker is located via `strstr`, so it can appear anywhere in the file -- not necessarily on the first line. Everything before it (comments, version metadata, other INI sections) is silently ignored.
- **Parsing starts at marker+7.** Exactly 7 characters (`[knobs]`) are skipped. The parse callback is `ParseKnobsString` (`sub_79B530`), which processes newline-delimited `key=value` pairs. The `~` separator and `WHEN=` conditional syntax are supported.
- **Result/Expected monad.** Every I/O operation has a corresponding error path. Errors are accumulated via `sub_79A3D0` (ErrorChainAppend) and propagated through a tagged result object. Multiple errors from a single file are chained, not short-circuited.

Error strings with source line numbers:

| Error String | Source Line | Condition |
|---|---|---|
| `"fseek() error knobsfile %s"` | 1075 | `fseek(SEEK_END)` or `fseek(SEEK_SET)` fails |
| `"fseek() error for knobsfile %s"` | 1080 | `fseek(SEEK_END)` fails (alternate path) |
| `"fread() error knobsfile %s"` | 1070 | `fread` returns <= 0 |
| `"Knobs header not found in %s"` | 1065 | `strstr(buffer, "[knobs]")` returns NULL |
| `"fclose() error for knobsfile %s"` | 1085 | `fclose` returns non-zero |

### Method 3: PTX Pragma

Knobs can be set from PTX source via `.pragma` directives, unless the `DisablePragmaKnobs` knob is set. The pragma string is copied into a temporary buffer and parsed by `ParseKnobsString` (`sub_79B530`), following the same key=value syntax.

### Method 4: WHEN= Conditional Overrides

The most powerful mechanism allows setting knobs conditionally, based on shader hash or instruction hash. The override string uses `~` (tilde) as a record separator:

```
WHEN=SH=0xDEADBEEF;SchedNumBB_Limit=200~WHEN=IH=0x12345;DisableCSE=1
```

`ParseKnobsString` (`sub_79B530`) recognizes these prefixes (case-insensitive):
- **`WHEN=`** -- conditional knob application
- **`SH=`** -- match by shader hash (decimal, hex with `0x`, or range with `..`)
- **`IH=`** -- match by instruction hash
- **`K=`** -- direct knob setting (no condition)
- **`INJECTSTRING`** -- special directive terminated by `;;` (double semicolon)

The full conditional override system is parsed by `ParseKnobOverrides` (`sub_79C210`), which iterates a linked list of override entries at `knob_state + 68904`. Each entry carries the condition (hash match criterion) and the knob assignment to apply when matched.

Hash matching uses FNV-1a (magic `0x811C9DC5`, prime `16777619`) for the per-function override table lookup at `ctx+120 → +1128`. See `IsPassDisabledFull` (`sub_7992A0`).

### Priority Order

When the same knob is set by multiple mechanisms, the last write wins. `KnobsInit` (`sub_79D990`) processes sources in this order:

1. Environment variable overrides (`getenv`)
2. Knobs file (if specified via `-knobs-file` or equivalent)
3. `-knob` CLI flags
4. PTX pragma knobs (applied per-function at compile time)
5. WHEN= conditional overrides (applied per-function when hash matches)

Later sources override earlier ones for the same knob index.

## Two Instantiations: DAG and OCG

The knob system is a C++ template instantiated twice with different knob definition tables:

### DAG Knobs (sub_6F0820)

The DAG (Directed Acyclic Graph) scheduler knob table contains 99 entries. These control the Mercury SASS pipeline: instruction expansion, WAR hazard handling, scoreboard configuration, and the decode/expand/opex pipeline stages.

| Property | Value |
|---|---|
| GetKnobIndex | `sub_6F0820` |
| ParseKnobValue | `sub_6F7360` |
| InitializeKnobs | `sub_6F68C0` (9KB, 24 references to `generic_knobs_impl.h`) |
| Table size | 99 entries x 64 bytes = 6,336 bytes |

DAG knobs referenced in the binary include knob indices 8 and 17 (pipeline options in `sub_6F52F0`), 16 (WAR generation options in `sub_6FBC20`), and 743/747 (expansion options in `sub_6FFDC0`).

### OCG Knobs (sub_79B240)

The OCG (Optimizing Code Generator) knob table contains 1,195 entries -- the vast majority of all knobs. These control the optimization passes, register allocation, instruction scheduling, and code generation.

| Property | Value |
|---|---|
| GetKnobIndex | `sub_79B240` |
| ParseKnobValue | `sub_79F540` |
| KnobsInit | `sub_79D990` (40,817 bytes, master initializer) |
| KnobInit | `sub_7A0C10` (per-knob state constructor) |
| Table size | 1,195 entries x 64 bytes = 76,480 bytes |
| Runtime values | 1,195 entries x 72 bytes = 86,040 bytes |

OCG knob indices referenced across the codebase include: 185 (pass-disable string, offset 13320), 294 (epilogue instruction count, used in tepid scheduling), 487 (LoopMakeSingleEntry enablement), 956-957 (shader hint settings at offsets 68832/68904).

## Knob State Object

The master knob state object is constructed by `KnobInit` (`sub_7A0C10`):

```
Offset    Size    Field
────────  ──────  ──────────────────────────────
+0        8       vtable pointer (off_21C0738)
+8        8       arena allocator
+16       8       knob definition table pointer
+24       8       knob count
+32       40      (zero-initialized control fields)
+72       var     knob value array (72 * count bytes)
+80       4       max knob index (initially 0xFFFFFFFF)
+88       16      DUMP_KNOBS_TO_FILE path (growable string)
```

The vtable at `off_21C0738` provides virtual methods for knob access:
- `vtable+72`: `IsKnobSet(index)` -- check if a knob has a value
- `vtable+152`: `GetKnobIntValue(index)` -- retrieve int32 value
- And others for bool, string, double retrieval

## Knob Access Helpers

Throughout the codebase, knobs are accessed by index via small helper functions:

| Function | Address | Purpose |
|---|---|---|
| `GetKnobIntValue` | `sub_7A1B80` | Returns `*(int32*)(state + 72*idx + 8)` |
| `GetKnobBoolValue` | `sub_7A1CC0` | Checks type == 4, returns presence |
| `GetKnobStringValue` | `sub_7A1E10` | Returns string pointer (type 5/8) |
| `SetKnobValue` | `sub_7A2860` | Writes value with optional WHEN=SH= condition |
| `IsKnobSet` | (inlined) | Checks `*(byte*)(state + 72*idx) != 0` |

Access is O(1) by index -- no hash lookup or name comparison at runtime. The `GetKnobIndex` name-to-index translation happens only during initialization.

## Pass Disable Mechanism

The knobs system provides a string-based pass disable mechanism through knob index 185 (OCG offset 13320). The string contains `+`-delimited pass names:

```
-knob DisablePhases=LoopMakeSingleEntry+SinkCodeIntoBlock
```

Two check functions consult this string:

### IsPassDisabled (sub_799250)

Simple version. Reads the disable flag byte at `ctx+13320`:
- If byte == 0: no pass-disable configured, returns false
- If byte == 5: string pointer at `ctx+13328`, performs substring match via `sub_6E1520` (strcasestr-like)

Called from 16+ sites across the codebase: `sub_78B430` (LoopMakeSingleEntry), `sub_78DB70` (SinkCodeIntoBlock), `sub_8236B0`, `sub_8D0640`, `sub_8F45E0`, and others.

### IsPassDisabledFull (sub_7992A0)

Full version with per-function overrides. First checks a per-function hash table at `ctx+120 → +1128` using FNV-1a on the function identifier. If the function has a specific override entry, reads the disable string from there. Otherwise falls back to the global disable string at `ctx+72 → +13320`.

```c
// FNV-1a hash for per-function lookup
uint32_t hash = 0x811C9DC5;
for (each byte b in function_id)
    hash = 16777619 * (hash ^ b);
uint32_t bucket = hash & (table_size - 1);
```

The `+` character is used as a delimiter between alternative phase names in the disable string, allowing `"phaseA+phaseB"` to match either name.

### NamedPhases Parser (sub_798B60)

Parses a comma-separated list of `name=value` pairs into parallel arrays (max 256 entries). Used by `KnobsInitFromEnv` (`sub_79C9D0`) to process environment variable-based knob overrides.

```
Input:  "knob1=value1,knob2=value2,knob3=value3"
Output: names[256], values[256], full_strings[256]
```

## Knob Categories

The 1,294 knobs cluster into functional categories. Prefix analysis of decoded knob names reveals these major groups:

| Prefix | Count | Domain |
|---|---|---|
| `Sched*` / `PostSched*` / `Sb*` | 89 | Instruction scheduling heuristics and thresholds |
| `RegAlloc*` / `Reg*` | 87 | Register allocation parameters, spill cost model, target selection |
| `Disable*` | 75 | Pass/feature disable switches (boolean) |
| `Remat*` / `SinkRemat*` | 35 | Rematerialization cost model, enable switches, placement control |
| `Mercury*` / `Merc*` | 21 | Mercury encoder configuration |
| `URF*` | 24 | Uniform Register File optimization |
| `Enable*` | 19 | Pass/feature enable switches (boolean) |
| `Dump*` | 15 | Debug dump controls (DUMPIR, DumpSched, etc.) |
| `Peephole*` | ~20 | Peephole optimization rules |
| `Loop*` | ~15 | Loop optimization parameters |
| `Sync*` / `Barrier*` | ~12 | Synchronization and barrier handling |
| `WAR*` | ~8 | Write-after-read hazard parameters |
| `GMMA*` / `MMA*` | ~10 | Matrix multiply-accumulate configuration |
| `Spill*` | ~8 | Spill code generation parameters |
| `Budget*` | ~10 | Cost model budgets (BDGT type knobs) |
| `Copy*` / `CSE*` | ~8 | Copy propagation and CSE parameters |
| (other) | ~577 | Miscellaneous per-pass tuning knobs |

### Notable Individual Knobs

Selected knobs referenced by address in the binary:

| Index | Name (decoded) | Type | Referenced At | Purpose |
|---|---|---|---|---|
| 8 | (DAG pipeline) | INT | `sub_6F52F0` | Pipeline option flag |
| 16 | (WAR generation) | INT | `sub_6FBC20` | WAR pass behavior |
| 17 | (DAG pipeline) | INT | `sub_6F52F0` | Pipeline option flag |
| 185 | (pass-disable string) | STR | `sub_799250`, `sub_7992A0` | DisablePhases string |
| 294 | (epilogue count) | INT | `sub_7A46E0` | Tepid scheduling divisor |
| 487 | (loop single-entry) | BOOL | `sub_78B430` | LoopMakeSingleEntry enable |
| 743 | (expansion option) | INT | `sub_6FFDC0` | Mercury expansion control |
| 747 | (expansion option) | INT | `sub_6FFDC0` | Mercury expansion control |
| 956 | (shader hint) | — | `sub_79C210` | Shader hint knob (offset 68832) |
| 957 | (shader hint) | — | `sub_79C210` | Shader hint linked list (offset 68904) |

### Register Allocation Knobs (87 knobs, indices 613--699)

The register allocator is the most heavily parameterized subsystem in ptxas. Its 87 knobs span indices 613 through 699 in the OCG knob table, registered in `ctor_005` at addresses `0x4197F0`--`0x41B2E0`. The knobs cluster into seven functional sub-categories. All names decoded from ROT13 strings at `0x21B9730`--`0x21BA6C0`.

#### A. Spill Cost Model (26 knobs)

The spill guidance engine (`sub_96D940`, 84 KB) uses these knobs to compute per-candidate spill costs. The model multiplies hardware-specific latency and resource metrics by configurable scale factors, then applies threshold-based activation logic.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 658 | `RegAllocSpillBarriersAcrossSuspend` | NONE | Enable spill barriers across suspend points |
| 659 | `RegAllocSpillBit` | INT | Master spill-bit mode selector |
| 660 | `RegAllocSpillBitHighRegCountHeur` | INT | High register count heuristic for spill-bit decisions |
| 661 | `RegAllocSpillBitHighRegScale` | DBL | Scale factor for high-register-count spill cost |
| 662 | `RegAllocSpillBitInfPerRegThreshold` | INT | Interference-per-register threshold for spill-bit activation |
| 663 | `RegAllocSpillBitLowRegCountHeur` | INT | Low register count heuristic for spill-bit decisions |
| 664 | `RegAllocSpillBitLowRegScale` | DBL | Scale factor for low-register-count spill cost |
| 665 | `RegAllocSpillBitMediumRegScale` | DBL | Scale factor for medium-register-count spill cost |
| 666 | `RegAllocSpillBitNonRematSpillThreshold` | INT | Threshold for non-rematerializable spill-bit activation |
| 667 | `RegAllocSpillBitRLivePerRegThreshold` | INT | Live-per-register threshold for R-type spill decisions |
| 668 | `RegAllocSpillBitRLiveThreshold` | INT | Global R-live threshold for spill activation |
| 669 | `RegAllocSpillForceXBlockHoistRefill` | INT | Force cross-block hoisting of refill instructions |
| 670 | `RegAllocSpillLatencyScale` | DBL | Scale factor for latency in spill cost model |
| 671 | `RegAllocSpillLatencyScale2` | DBL | Secondary latency scale (nested loops) |
| 672 | `RegAllocSpillMemResScale` | DBL | Scale factor for memory resource pressure in spill cost |
| 673 | `RegAllocSpillMioHeavyThreshold` | DBL | Threshold for MIO-heavy (memory-intensive) spill classification |
| 674 | `RegAllocSpillOptBudget` | BDGT | Budget for spill optimization passes |
| 675 | `RegAllocSpillResourceScale` | DBL | Scale factor for resource usage in spill cost |
| 676 | `RegAllocSpillResCostsScale` | DBL | Scale factor for resource costs (secondary weighting) |
| 677 | `RegAllocSpillReturnRegister` | INT | Spill handling mode for return-value registers |
| 678 | `RegAllocSpillSmemFlatMode` | INT | Shared memory spill: flat addressing mode selector |
| 679 | `RegAllocSpillSmemLatencyScale` | DBL | Scale factor for shared-memory spill latency |
| 680 | `RegAllocSpillTexDepScale` | DBL | Scale factor for texture dependency in spill cost |
| 681 | `RegAllocSpillValidateDebug` | INT | Debug: validate spill correctness (0=off, >0=level) |
| 682 | `RegAllocSpillXBlock` | INT | Cross-block spill mode (hoist/refill strategy) |
| 683 | `RegAllocSpillXBlock2` | INT | Secondary cross-block spill mode |

The cost model uses three register-count tiers (low/medium/high), each with independent scale factors (664, 665, 661). The tier boundaries are set by the heuristic knobs (663, 660). Latency scales (670, 671) multiply the estimated stall cycles, while resource scales (672, 675, 676) multiply memory bandwidth consumption. The MIO-heavy threshold (673) triggers a separate cost path when the basic block is already saturated with memory operations.

#### B. Rematerialization (11 knobs)

Rematerialization recomputes values instead of spilling them. The allocator treats remat as a first-class spill alternative with its own budget and candidate ordering.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 619 | `RegAllocCtxSensitiveRemat` | INT | Enable context-sensitive rematerialization |
| 622 | `RegAllocEnableOptimizedRemat` | INT | Enable optimized rematerialization pass |
| 627 | `RegAllocLiveRemat` | INT | Enable live-range-aware rematerialization |
| 632 | `RegAllocMaxRematHeight` | INT | Max expression DAG height for remat candidates |
| 633 | `RegAllocMaxRematInst` | INT | Max instructions in a remat sequence |
| 635 | `RegAllocMultiRegclassRemat` | INT | Enable remat across multiple register classes |
| 636 | `RegAllocMultiRegRemat` | INT | Enable multi-register rematerialization |
| 637 | `RegAllocMultiRegRematBudget` | BDGT | Budget for multi-register remat attempts |
| 650 | `RegAllocRematDisableRange` | IRNG | Disable remat for instruction index range `lo..hi` |
| 651 | `RegAllocRematEnable` | INT | Master enable for rematerialization (0=off) |
| 652 | `RegAllocRematReuseBudget` | BDGT | Budget for remat-reuse optimization attempts |
| 654 | `RegAllocOrderRematCandHeuristic` | INT | Heuristic for ordering remat candidates |

Knob 650 (`RegAllocRematDisableRange`) is unique as the only IRNG-type knob in the set, accepting `"lo..hi"` to disable rematerialization for a range of instruction indices -- a debugging aid for bisecting remat-related miscompiles.

#### C. Pre-Assignment / MAC (8 knobs)

MAC (Machine-level Allocation with Constraints) pre-assigns physical registers to high-priority operands before the main Fatpoint allocator runs. Entry: `sub_94A020` (331 lines).

| Index | Name | Type | Purpose |
|---|---|---|---|
| 613 | `RegAllocAvoidBankConflictMac` | INT | Enable bank-conflict-aware MAC pre-assignment |
| 614 | `RegAllocAvoidBankConflictMacPenalty` | INT | Penalty weight for bank conflicts during MAC pre-assignment |
| 615 | `RegAllocAvoidBankConflictMacWindowSize` | INT | Instruction window size for bank conflict analysis |
| 628 | `RegAllocMacForce` | NONE | Force MAC-level pre-allocation path |
| 629 | `RegAllocMacVregAllocOrder` | INT | Vreg processing order during MAC allocation |
| 630 | `RegAllocMacVregAllocOrderCompileTime` | INT | Compile-time variant of MAC vreg allocation order |
| 646 | `RegAllocPrefMacOperands` | INT | MAC operand preference level (1=read, 2=write, 3=both) |
| 647 | `RegAllocPrefMacOperandsMaxDepth` | INT | Max operand chain depth for MAC preference propagation |

#### D. Coalescing (3 knobs)

Register coalescing eliminates unnecessary register-to-register copies by merging live ranges.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 617 | `RegAllocCoalesceBudget` | BDGT | Budget limit for coalescing iterations |
| 618 | `RegAllocCoalescing` | NONE | Enable register coalescing |
| 634 | `RegAllocMmaCoalescing` | NONE | Enable MMA-specific coalescing |

#### E. Performance-Difference Backoff (5 knobs)

Progressive constraint relaxation: on retry iteration N, if the performance difference exceeds a limit, constraints relax between the begin and end iterations.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 641 | `RegAllocPerfDiffBackoff` | NONE | Enable perf-diff based constraint backoff |
| 642 | `RegAllocPerfDiffBackoffBegin` | INT | Iteration at which backoff begins |
| 643 | `RegAllocPerfDiffBackoffEnd` | INT | Iteration at which full relaxation is reached |
| 644 | `RegAllocPerfDiffConflictWeight` | INT | Weight factor for conflicts in perf-diff calculation |
| 645 | `RegAllocPerfDiffLimit` | INT | Performance difference limit triggering relaxation |

#### F. Register Target Selection (13 knobs)

The target selection phase determines how many physical registers to aim for -- the occupancy/performance tradeoff. More registers per thread means fewer warps can execute concurrently.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 687 | `RegTargetList` | ILIST | Comma-separated list of target register counts to try |
| 688 | `RegTgtLowerLimitMMASlack` | INT | Slack added to MMA lower register limit |
| 689 | `RegTgtLowerLimitTCGENSlack` | INT | Slack added to TCGEN lower register limit |
| 690 | `RegTgtLowerLimitSPARSIFYSlack` | INT | Slack added to SPARSIFY lower register limit |
| 691 | `RegTgtLowerLimitDECOMPRESSSlack` | INT | Slack added to DECOMPRESS lower register limit |
| 692 | `RegTgtSelHigherWarpCntHeur` | INT | Heuristic mode for higher-warp-count target selection |
| 693 | `RegTgtSelHigherWarpCntHeurValue` | DBL | Weight value for higher-warp-count heuristic |
| 694 | `RegTgtSelHighLiveRangeHeurValue` | DBL | Weight for high-live-range target selection heuristic |
| 695 | `RegTgtSelLowerWarpCntHeur` | INT | Heuristic mode for lower-warp-count target selection |
| 696 | `RegTgtSelLowerWarpCntHeurValue` | DBL | Weight value for lower-warp-count heuristic |
| 697 | `RegTgtSelLowLiveRangeHeurValue` | DBL | Weight for low-live-range target selection heuristic |
| 698 | `RegTgtSelWithSMemSpillHeur` | INT | Heuristic mode when shared-memory spilling is active |
| 699 | `RegUsageLevel` | INT | Register usage reporting level |

The four "Slack" knobs (688--691) fine-tune lower register limits for specific architectural features that have minimum register requirements: MMA (matrix multiply), TCGEN (tensor core generation), SPARSIFY (structured sparsity), DECOMPRESS (decompression).

#### G. General Allocation Control (12 knobs)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 616 | `RegAllocCacheSize` | INT | Cache size parameter for interference graph |
| 620 | `RegAllocDebugConflictDetails` | INT | Debug: print conflict graph details (verbosity level) |
| 621 | `RegAllocDepDistanceThresholdForHighConflicts` | INT | Dep-distance threshold above which high-conflict registers are deprioritized |
| 624 | `RegAllocIndexAbiScratchRegs` | INT | Index into ABI scratch register set |
| 639 | `RegAllocNumNonSpillTrials` | INT | Non-spill allocation trials before allowing spills |
| 640 | `RegAllocOptLevel` | INT | Regalloc optimization level (controls aggressiveness) |
| 648 | `RegAllocPrintDetails` | NONE | Enable detailed regalloc diagnostic printing |
| 649 | `RegAllocRefineInf` | INT | Refine interference graph iteration limit |
| 653 | `RegAllocOptimizeABI` | INT | Enable ABI-aware register optimization (setmaxnreg handling) |
| 655 | `RegAllocReportMaxRegsAllowed` | INT | Report maximum registers allowed per thread (diagnostic) |
| 656 | `RegAllocCudaSmemSpillEnable` | INT | Enable CUDA shared memory spill path |
| 685 | `RegAllocUserSmemBytesPerCTA` | INT | User-specified shared memory bytes per CTA (overrides computed) |

#### H. Miscellaneous (8 knobs)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 623 | `RegAllocEstimatedLoopIterations` | STR | String hint providing estimated loop iteration counts for spill cost weighting |
| 625 | `RegAllocL1SpillRegThres` | INT | Register count threshold for L1 spill mode activation |
| 626 | `RegAllocL1SpillScale` | DBL | Scale factor for L1 cache spill cost |
| 631 | `RegAllocMaxGmmaDisallowedReg` | INT | Max registers disallowed during GMMA (warp group MMA) allocation |
| 638 | `RegAllocNoRetargetPrefs` | NONE | Disable retarget-preference optimization |
| 657 | `RegAllocSortRegs` | INT | Sorting order for register candidates during allocation |
| 684 | `RegAllocThresholdForDiscardConflicts` | INT | Interference count above which conflicts are discarded (default 50) |
| 686 | `RegAttrReuseVectorBudget` | BDGT | Budget for register-attribute vector reuse optimization |

### Scheduling Knobs (89 knobs, indices 229--978)

The instruction scheduler is the second most heavily parameterized subsystem after register allocation. Its 89 knobs span two contiguous blocks (indices 738--811 for the core `Sched*` set, and 569--574 for the `PostSched*` set) plus 11 scattered entries for scheduling-adjacent features. All names decoded from ROT13 strings at `0x21B6CB0`--`0x21BE100`, registered in `ctor_005` at code addresses `0x411FF0`--`0x420A00`.

The knobs control every aspect of the list scheduler: how latencies are modeled, which functional units are treated as busy, how aggressively cross-block motion is attempted, and how register pressure feedback loops interact with the priority function. Three Blackwell-era `SchedResBusy*` knobs (QMMA at 964, OMMA at 977, MXQMMA at 978) sit outside the main block because they were appended in a later toolkit version for new MMA unit types.

#### A. Resource Busy Overrides (28 knobs)

The `SchedResBusy*` knobs override the hardware-profile resource busy times for individual functional units. Each knob sets the number of cycles the named unit is considered occupied after issuing an instruction to it. When unset, the scheduler uses the value from the latency model's per-SM hardware profile. Setting a `SchedResBusy*` knob to 0 effectively makes the unit appear always free to the scheduler.

Two knobs accept string values instead of integers: `SchedResBusyOp` and `SchedResBusyMachineOpcode` take a string identifying a specific opcode or machine opcode to override, enabling per-instruction busy-time tuning.

| Index | Name | Type | Functional Unit |
|---|---|---|---|
| 781 | `SchedResBusyADU` | INT | Address divergence unit |
| 782 | `SchedResBusyALU` | INT | Arithmetic logic unit |
| 783 | `SchedResBusyCBU` | INT | Convergence barrier unit |
| 784 | `SchedResBusyDMMA` | INT | Double-precision MMA unit |
| 785 | `SchedResBusyFMA` | INT | Fused multiply-add unit |
| 786 | `SchedResBusyFMAWide` | INT | Wide FMA unit (multi-cycle) |
| 787 | `SchedResBusyFP16` | INT | Half-precision FP unit |
| 788 | `SchedResBusyFP64` | INT | Double-precision FP unit |
| 789 | `SchedResBusyGMMA` | INT | Warp group MMA (WGMMA) unit |
| 790 | `SchedResBusyHMMA16` | INT | Half-precision MMA, 16-wide |
| 791 | `SchedResBusyHMMA16816` | INT | Half-precision MMA, 16x8x16 shape |
| 792 | `SchedResBusyHMMA1688` | INT | Half-precision MMA, 16x8x8 shape |
| 793 | `SchedResBusyHMMA32` | INT | Half-precision MMA, 32-wide |
| 794 | `SchedResBusyIMMA` | INT | Integer MMA unit |
| 795 | `SchedResBusyLSU` | INT | Load/store unit |
| 796 | `SchedResBusyLSUL1` | INT | Load/store unit (L1 path) |
| 797 | `SchedResBusyOp` | STR | Per-opcode override (string: opcode name) |
| 798 | `SchedResBusyMachineOpcode` | STR | Per-machine-opcode override (string) |
| 799 | `SchedResBusyUDP` | INT | Uniform datapath unit |
| 800 | `SchedResBusyXU64` | INT | Extended-precision (64-bit) unit |
| 964 | `SchedResBusyQMMA` | INT | Quarter-precision MMA unit (Blackwell) |
| 977 | `SchedResBusyOMMA` | INT | Octal MMA unit (Blackwell) |
| 978 | `SchedResBusyMXQMMA` | INT | MX-quantized MMA unit (Blackwell) |

The five HMMA variants (790--793) correspond to different tensor core shapes: `HMMA16` for 16-wide half-precision, `HMMA1688` for the 16x8x8 tile used on Volta/Turing, `HMMA16816` for the 16x8x16 tile used on Ampere+, and `HMMA32` for 32-wide half-precision operations. IMMA (794) handles integer tensor operations (INT8/INT4).

#### B. Latency Overrides (12 knobs)

These override the default latency values the scheduler uses for dependency edges. The `SchedRead*` prefix indicates read-after-write latencies; the `SchedTex*` and `SchedLDS*` variants target texture and shared-memory operations specifically.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 757 | `SchedLDSLatency` | INT | Shared memory (LDS) load latency in cycles |
| 771 | `SchedReadLatency` | INT | Default read-after-write latency |
| 772 | `SchedReadSBBaseLatency` | INT | Scoreboard base read latency |
| 773 | `SchedReadSBBaseUseLSULat` | BOOL | Use LSU latency as scoreboard base |
| 774 | `SchedReadSbDmmaLatency` | INT | Scoreboard read latency for DMMA operations |
| 775 | `SchedReadSbLdgstsLatency` | INT | Scoreboard read latency for LDGSTS (async copy) operations |
| 802 | `SchedSyncsLatency` | INT | Synchronization barrier latency |
| 803 | `SchedSyncsPhasechkLatency` | INT | Phase-check synchronization latency |
| 804 | `SchedTex2TexIssueRate` | INT | Minimum cycles between back-to-back texture issues |
| 808 | `SchedTexLatency` | INT | Texture fetch latency in cycles |
| 811 | `SchedXU64Latency` | INT | Extended 64-bit unit latency |
| 770 | `SchedReadAvailTarget` | INT | Target availability delay for read operands |

#### C. Register Pressure Feedback (8 knobs)

The scheduler's priority function incorporates register pressure awareness through these knobs. They control how aggressively the scheduler tries to reduce live register count: `SchedMaxRTarget` sets the target register count, while the `SchedMaxRLive*` knobs define slack bands around that target. `SchedReduceIncLimit*` throttles how quickly the scheduler increases its pressure-reduction efforts.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 758 | `SchedLocalRefRatio` | DBL | Local reference ratio weight in priority function |
| 760 | `SchedMaxRLiveCarefulSlack` | INT | Slack before aggressive register pressure reduction |
| 761 | `SchedMaxRLiveOKslack` | INT | Slack band where register pressure is acceptable |
| 762 | `SchedMaxRLiveOKslackColdBlocks` | INT | OK-slack for cold (infrequently executed) blocks |
| 763 | `SchedMaxRTarget` | INT | Target maximum register count for scheduling |
| 776 | `SchedReduceIncLimit` | INT | Limit on incremental register pressure reduction steps |
| 778 | `SchedReduceIncLimitHigh` | INT | Upper bound on incremental reduction |
| 779 | `SchedReduceRegBudget` | BDGT | Budget for register-pressure-reduction iterations |

#### D. Cross-Block Scheduling (8 knobs)

Cross-block motion allows the scheduler to move instructions across basic block boundaries for better latency hiding. These knobs control the scope and cost limits of cross-block speculation.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 742 | `SchedCrossBlock` | INT | Master cross-block scheduling mode selector |
| 743 | `SchedCrossBlockInstsToSpeculate` | INT | Max instructions to speculate across block boundary |
| 744 | `SchedCrossBlockLimit` | INT | Overall cross-block motion limit |
| 745 | `SchedCrossBlockSpeculate` | INT | Speculation mode for cross-block motion |
| 746 | `SchedCrossBlockSpeculateBudget` | BDGT | Budget for cross-block speculation attempts |
| 747 | `SchedCrossBlockTexToSpeculate` | INT | Max texture instructions to speculate across blocks |
| 288 | `EnableXBlockSchedInMultiBlockInMMALoop` | INT | Enable cross-block scheduling within multi-block MMA loops |
| 738 | `SbXBlock` | INT | Cross-block scoreboard tracking mode |

#### E. Texture Batching (7 knobs)

Texture operations have high latency, so the scheduler groups them into batches to maximize memory-level parallelism. These knobs control batch formation and target selection.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 741 | `SchedCountLoadsPerTex` | INT | Max loads to count per texture operation |
| 756 | `SchedLDGBatchDelayBias` | INT | Delay bias for global load batching |
| 755 | `SchedLastHybridInBBWithIssueRate` | INT | Last hybrid scheduler position in BB with issue rate |
| 805 | `SchedTexBatchTargetSelectRegisterTarget` | INT | Batch formation: prefer register-target-aware grouping |
| 806 | `SchedTexBatchTargetSelectSchedulerTarget` | INT | Batch formation: prefer scheduler-target grouping |
| 807 | `SchedTexBatchTargetTexReadTogether` | INT | Batch formation: prefer grouping tex reads together |
| 931 | `UseGroupOpexesForResourceScheduling` | INT | Use grouped opexes for resource scheduling decisions |

#### F. Dependency Modeling (6 knobs)

These control how the scheduler builds and refines the dependency graph between instructions.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 753 | `SchedAddDepFromGlobalMembarToCB` | INT | Add dependency edge from global membar to CB unit |
| 759 | `SchedMaxMemDep` | INT | Max memory dependencies per instruction |
| 764 | `SchedMemNoAlias` | NONE | Assume no memory aliasing (aggressive scheduling) |
| 777 | `SchedReduceRefPsuedoDepLimit` | INT | Limit on reducing reference pseudo-dependencies |
| 780 | `SchedRefineMemDepBudget` | BDGT | Budget for memory dependency refinement iterations |
| 801 | `SchedSymmetricAntiDepConflictWindow` | BOOL | Enable symmetric anti-dependency conflict window |

#### G. Post-Scheduler (6 knobs)

The post-scheduler runs after register allocation (phase 103) and adjusts the schedule to account for actual register assignments. It primarily inserts stall cycles and adjusts issue delays.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 569 | `PostSchedAdvLatencyHiding` | BOOL | Enable advanced latency hiding in post-scheduler |
| 570 | `PostSchedBudget` | BDGT | Budget for post-scheduler iterations |
| 571 | `PostSchedEarlyStall` | INT | Early stall insertion mode |
| 572 | `PostSchedForceReverseOrder` | INT | Force reverse traversal order in post-scheduler |
| 573 | `PostSchedIssueDelay` | BOOL | Enable issue delay computation |
| 574 | `PostSchedIssueDelayForNoWBStalls` | BOOL | Compute issue delays for no-writeback stalls |

#### H. Ordering and Preservation (5 knobs)

These control whether the scheduler preserves the original instruction order (from the optimizer or PTX source) versus reordering freely.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 229 | `ForcePreserveSchedOrderSameNvOpt` | INT | Force preserve scheduling order from NvOpt pass |
| 594 | `PreserveSchedOrder` | NONE | Preserve source scheduling order (boolean) |
| 595 | `PreserveSchedOrderSame` | BOOL | Preserve scheduling order for same-priority instructions |
| 751 | `SchedForceReverseOrder` | INT | Force reverse scheduling order (bottom-up) |
| 769 | `SchedPrefFurthestDep` | BOOL | Prefer instructions with furthest dependency |

#### I. Scoreboard (4 knobs)

The hardware scoreboard tracks instruction completion. These knobs tune how the scheduler predicts scoreboard occupancy to avoid stalls.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 738 | `SbXBlock` | INT | Cross-block scoreboard tracking mode |
| 739 | `SbXBlockLLSB` | INT | Cross-block long-latency scoreboard tracking |
| 772 | `SchedReadSBBaseLatency` | INT | Scoreboard base read latency |
| 773 | `SchedReadSBBaseUseLSULat` | BOOL | Use LSU latency as scoreboard base |

Note: `SbXBlock` appears in both cross-block (D) and scoreboard (I) categories because it serves both purposes -- it controls whether the scoreboard state propagates across block boundaries, which is a prerequisite for cross-block scheduling correctness.

#### J. MMA Coupling (3 knobs)

Matrix multiply-accumulate instructions on certain architectures share functional unit resources. These knobs control how the scheduler models coupled execution.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 752 | `SchedFP16CoupledMaxellPascal` | INT | FP16 coupled execution mode on Maxwell/Pascal |
| 754 | `SchedHmmaImmaBmmaCoupledAmperePlus` | INT | HMMA/IMMA/BMMA coupled execution on Ampere+ |
| 366 | `GroupOpexesForResourceSchedulingThreshold` | DBL | Threshold for grouping opexes in resource scheduling |

#### K. Scheduler Model (4 knobs)

These control how the scheduler models the hardware pipeline and instruction movement costs.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 765 | `SchedModelIdentityMove` | INT | Model identity moves as zero-latency |
| 766 | `SchedModelSharedPhysicalPipe` | INT | Model shared physical pipe contention |
| 767 | `SchedMultiRefDeltaLive` | INT | Delta-live threshold for multi-reference instructions |
| 768 | `SchedMultiRefDeltaLiveMinRefs` | INT | Minimum reference count for delta-live calculation |

#### L. Budget, Scale, and Control (7 knobs)

General scheduling control knobs covering budgets, loop iteration estimates, the master disable switch, and validation.

| Index | Name | Type | Purpose |
|---|---|---|---|
| 740 | `SchedBumpScaleAugmentFactor` | DBL | Augment factor for priority bump scaling |
| 748 | `SchedDisableAll` | INT | Master disable for all scheduling passes |
| 749 | `SchedDynBatchBudget` | BDGT | Budget for dynamic batching iterations |
| 750 | `SchedEstimatedLoopIterations` | STR | Estimated loop iterations (string: per-loop hints) |
| 809 | `ScheduleKILs` | INT | Schedule KIL (kill/discard) instructions |
| 810 | `SchedValidateLiveness` | INT | Enable liveness validation after scheduling |
| 811 | `SchedXU64Latency` | INT | XU64 unit latency override |

### Disable Switches (75 knobs)

The disable switches are boolean knobs that turn off specific passes, optimizations, or workarounds. All 75 knobs containing "Disable" were decoded from ROT13 strings at `0x21BDE30`--`0x21BFA10`. Nearly all are `OKT_NONE` (boolean) type -- setting them with no value or any value disables the corresponding feature. The single exception is `RegAllocRematDisableRange`, which is `OKT_IRNG` and accepts a `"lo..hi"` instruction index range.

The bare `Disable` knob at `0x21BE860` appears to be a master pass-disable switch. `SchedDisableAll` is the master scheduler disable. `DisablePragmaKnobs` prevents PTX `.pragma` directives from setting knobs -- a meta-level control that protects the knob system itself.

#### A. Workaround (WAR) Switches (9 knobs)

These disable hardware or compiler bug workarounds. Each `War_SW*` knob corresponds to an NVIDIA internal bug tracker ID. Disabling a WAR reverts to the unpatched behavior -- useful for bisecting whether a WAR is causing a regression.

| Name | Feature Disabled |
|---|---|
| `DisableWar_SW200655588` | Workaround for bug SW-200655588 |
| `DisableWar_SW2549067` | Workaround for bug SW-2549067 |
| `DisableWar_SW2789503` | Workaround for bug SW-2789503 |
| `DisableWar_SW2965144` | Workaround for bug SW-2965144 |
| `DisableWar_SW3093632` | Workaround for bug SW-3093632 |
| `DisableForwardProgressWar1842954` | Forward-progress guarantee workaround (bug 1842954) |
| `DisableForwardProgressWar1842954ForDeferBlocking` | Same WAR, variant for defer-blocking scheduling |
| `DisableHMMARegAllocWar` | HMMA (half-precision MMA) register allocation workaround |
| `DisableMultiViewPerfWAR` | Multi-view rendering performance workaround |

#### B. Memory and Addressing (11 knobs)

These control address computation, memory access conversion, and shared-memory optimizations.

| Name | Feature Disabled |
|---|---|
| `DisableCvtaForGenmemToSmem` | Generic-to-shared address space conversion via `cvta` |
| `DisableDoubleIndexedAddress` | Double-indexed addressing mode optimization |
| `DisableErrbarAfterMembar` | Error barrier (`BAR.SYNC 15`) insertion after `membar.sys` |
| `DisableForceLDCTOLDCUConv` | LDC to LDCU (constant uniform load) conversion |
| `DisableImplicitMemDesc` | Implicit memory descriptor inference |
| `DisableLDCU256` | LDCU.256 -- 256-bit constant uniform load |
| `DisableLDCUWithURb` | LDCU with uniform register base addressing |
| `DisableLongIntArithAddressFolding` | Long integer arithmetic folding into address computation |
| `DisableRemoveSmemLea` | Shared memory LEA (load effective address) removal |
| `DisableSmemSizePerCTACheck` | Shared memory size per CTA validation check |
| `DisableStrideOnAddr` | Stride-on-address optimization (base+stride*index folding) |

#### C. Register Allocation and Uniform Registers (9 knobs)

These control uniform register (UR) file usage, live range management, and remat-related disable ranges.

| Name | Type | Feature Disabled |
|---|---|---|
| `DisableConvergentWriteUR` | NONE | Convergent write-to-UR optimization |
| `DisableExtendedLiveRange` | NONE | Extended live range optimization |
| `DisableU128` | NONE | 128-bit uniform register support |
| `DisableURLiveAcrossConvBound` | NONE | UR liveness across convergence boundaries |
| `DisableURLivenessTradeOff` | NONE | UR liveness trade-off heuristic |
| `DisableUreg` | NONE | Uniform register file usage entirely |
| `MercuryDisableLegalizationOfTexToURBound` | NONE | Mercury tex-to-UR-bound legalization |
| `RegAllocRematDisableRange` | IRNG | Rematerialization for instruction index range `lo..hi` |
| `RematDisableTexThrottleRegTgt` | NONE | Texture throttle register target during remat |

#### D. Loop Optimization (6 knobs)

| Name | Feature Disabled |
|---|---|
| `DisableAlignHotLoops` | Hot loop alignment (NOP padding for fetch efficiency) |
| `DisableDeadLoopElimination` | Dead loop elimination pass |
| `DisableLoopLevelVaryingAnalysis` | Loop-level varying/invariant analysis |
| `DisableLoopPrecheckForYields` | Loop pre-check insertion for yield points (cooperative groups) |
| `DisableMeshVCTALoop` | Mesh shader virtual CTA loop optimization |
| `DisablePartialUnrollOverflowCheck` | Overflow check during partial loop unrolling |

#### E. Code Motion and Scheduling (6 knobs)

| Name | Feature Disabled |
|---|---|
| `DisableLatTransitivity` | Latency transitivity in scheduling dependency chains |
| `DisableMoveCommoning` | MOV-based equivalence propagation (commoning walker) |
| `DisableNestedHoist` | Nested code hoisting (loop-invariant-like motion) |
| `DisableOffDeck` | Off-deck scheduling (prefetch to off-deck buffer) |
| `DisableSourceOrder` | Source-order scheduling constraint |
| `SchedDisableAll` | **Master switch:** all scheduling passes |

#### F. Vectorization (4 knobs)

| Name | Feature Disabled |
|---|---|
| `DisableFastvecEnhancement` | Fast vectorization enhancement pass |
| `DisableHalfPartialVectorWrites` | Half-precision partial vector write coalescing |
| `DisableReadVectorization` | Load vectorization (coalescing scalar reads into vector loads) |
| `DisableWriteVectorization` | Store vectorization (coalescing scalar writes into vector stores) |

#### G. Predication and Branching (4 knobs)

| Name | Feature Disabled |
|---|---|
| `CmpToMovPredCrossBlockDisable` | CMP-to-MOV predicate propagation across basic blocks |
| `DisableBranchPredInput` | Branch predicate input optimization |
| `DisableCmpToPred` | CMP-to-predicate conversion |
| `DisablePredication` | Predication pass (phase 63, `OriDoPredication`) |

#### H. Synchronization and Barriers (2 knobs)

| Name | Feature Disabled |
|---|---|
| `DisableRedundantBarrierRemoval` | Redundant barrier removal pass |
| `DisableStageAndFence` | Stage-and-fence synchronization insertion |

#### I. Dead Code and Store Elimination (2 knobs)

| Name | Feature Disabled |
|---|---|
| `DisableDeadStoreElimination` | Dead store elimination pass |
| `DisableStraightenInSimpleLiveDead` | Straightening within simple live/dead analysis |

#### J. Control Flow Merging (5 knobs)

| Name | Feature Disabled |
|---|---|
| `DisableEarlyExtractBCO` | Early extraction of BCO (branch code optimization objects) |
| `DisableMergeEquivalentConditionalFlow` | Phase 133: tail merging of equivalent conditional branches |
| `DisableMergeFp16MovPhi` | FP16 MOV-PHI merge optimization |
| `DisableMergeSamRamBlocks` | SAM/RAM block merging (surface/texture access coalescing) |
| `DisableOptimizeHotColdFlow` | Hot/cold flow optimization (code layout splitting) |

#### K. Pass Control (2 knobs)

| Name | Feature Disabled |
|---|---|
| `Disable` | Master disable switch (bare name) |
| `DisablePragmaKnobs` | PTX `.pragma`-based knob overrides |

#### L. Sanitizer (3 knobs)

These control the address sanitizer instrumentation for different memory spaces. When the sanitizer is active, these knobs can selectively disable checking for one space while keeping the others.

| Name | Feature Disabled |
|---|---|
| `SanitizeDisableGlobal` | Address sanitizer for global memory accesses |
| `SanitizeDisableLocal` | Address sanitizer for local memory accesses |
| `SanitizeDisableShared` | Address sanitizer for shared memory accesses |

#### M. Floating Point (2 knobs)

| Name | Feature Disabled |
|---|---|
| `FPFoldDisable` | Floating-point constant folding |
| `FPRefactoringDisable` | Floating-point expression refactoring |

#### N. Miscellaneous (10 knobs)

| Name | Feature Disabled |
|---|---|
| `DisableBW225LongIntArith` | BW225 (Blackwell) long integer arithmetic optimization |
| `DisableBptTrapNoReturn` | BPT.TRAP no-return semantics (debugger breakpoint trap) |
| `DisableDependentConstExpr` | Dependent constant expression optimization |
| `DisableISBESharing` | ISBE (indexed set buffer entry) sharing for bindless textures |
| `DisableMarkF2FPackbTo16Bit` | Marking F2F.PACKB as 16-bit operation |
| `DisableNonUniformQuadDerivatives` | Non-uniform quad derivative computation |
| `DisablePadding` | NOP padding insertion (alignment and scheduling) |
| `DisablePicCodeGen` | Position-independent code generation |
| `DisableSopSr` | SOP (scalar operation) on special registers (SR) |
| `DisableSuperUdp` | Super-UDP (enhanced uniform datapath) optimization |

### Rematerialization Knobs (35 knobs)

Rematerialization knobs control the three dedicated remat pipeline phases (Phase 28: SinkRemat, Phase 69: OriDoRemat) and the cost model that decides whether recomputing a value is cheaper than keeping it live in a register. These are separate from the 12 `RegAlloc*Remat*` knobs documented above in [section B](#b-rematerialization-11-knobs), which control allocator-integrated rematerialization. The distinction matters: allocator-integrated remat fires during register allocation itself (sub_93AC90), while these knobs tune the standalone pre-allocation and post-predication remat passes.

The 35 knobs split into two contiguous blocks in the descriptor table plus one outlier:

- **Remat\*** (27 knobs, indices 702--728): Late rematerialization (Phase 69) and shared cost model
- **SinkRemat\*** (8 knobs, indices 824--831): Early sink+remat (Phase 28)

#### A. Remat Enable/Disable (5 knobs)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 709 | `RematDisableTexThrottleRegTgt` | INT | Disable texture-throttle register targeting during remat |
| 710 | `RematEarlyEnable` | INT | Enable Phase 54 early remat mode activation |
| 711 | `RematEnable` | INT | Master enable for Phase 69 late rematerialization |
| 712 | `RematEnablePReg` | NONE | Enable predicate register rematerialization (boolean flag) |
| 726 | `RematStressTest` | NONE | Force all remat candidates to be rematerialized (debug, boolean flag) |

Knob 711 (`RematEnable`) is the master switch. When zeroed via `-knob RematEnable=0`, Phase 69 skips its core loop entirely. Knob 710 (`RematEarlyEnable`) independently controls Phase 54's mode flag write (`ctx+1552 = 4`). Knob 726 (`RematStressTest`) is a debug-only boolean that forces every candidate to be rematerialized regardless of profitability -- useful for stress-testing correctness.

#### B. Remat Cost Model (10 knobs)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 702 | `RematAbsCostFactor` | DBL | Absolute cost scaling factor for remat profitability |
| 703 | `RematBackOffRegTargetFactor` | DBL | Back-off factor for register pressure target during remat |
| 705 | `RematColdBlockRatio` | DBL | Cost discount ratio for cold (rarely executed) blocks |
| 713 | `RematGlobalCostFactor` | DBL | Global cost multiplier for cross-block rematerialization |
| 714 | `RematGlobalLowCostFactor` | DBL | Cost factor for low-cost (cheap ALU: MOV, IADD, LOP3) remat |
| 716 | `RematLdcCost` | DBL | Cost weight assigned to LDC (load-from-constant-bank) remat |
| 719 | `RematMemCost` | DBL | Cost weight for memory-sourced (LD/ST) rematerialization |
| 722 | `RematReadUAsLdc` | INT | Treat uniform address reads as LDC for cost classification |
| 727 | `RematTexInstRatioThreshold` | DBL | Texture instruction ratio threshold for throttle activation |
| 728 | `RematTexThrottleRegTgtScale` | DBL | Scale factor for register target when texture throttle is active |

These 10 knobs parameterize the remat profitability function (`sub_90B790`). The cost model computes `remat_cost = instruction_cost * factor` and compares against register savings. The DBL-typed knobs (8 of 10) are floating-point multipliers that allow fine-grained tuning. The texture-specific knobs (727, 728) implement a throttle: when the ratio of texture instructions exceeds the threshold, the register target is scaled to avoid excessive register use that would harm texture unit throughput.

#### C. Register Pressure Control (5 knobs)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 706 | `RematConservativeRegSlack` | INT | Extra registers to reserve beyond target (conservative mode) |
| 708 | `RematCostRegLimit` | INT | Max register count considered during cost analysis |
| 718 | `RematMaxRegCount` | INT | Absolute ceiling on registers for remat decisions |
| 723 | `RematRegTargetFactor` | DBL | Scaling factor for computing the register pressure target |
| 724 | `RematRegTargetTrialLimit` | INT | Max iterations when searching for optimal register target |

The register target is the pressure level below which rematerialization becomes profitable. `RematRegTargetFactor` (723) scales the occupancy-derived target. `RematRegTargetTrialLimit` (724) caps the binary-search iterations in the target-finding loop. `RematMaxRegCount` (718) is a hard ceiling -- if current pressure exceeds this value, the remat pass operates in aggressive mode.

#### D. Instruction and Code Limits (2 knobs)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 707 | `RematCostInstLimit` | INT | Max instruction count for inclusion in cost model |
| 715 | `RematInflationSlack` | INT | Allowed code-size inflation slack (extra instructions from remat) |

`RematCostInstLimit` (707) prevents the cost model from analyzing extremely large remat sequences. `RematInflationSlack` (715) limits how many extra instructions rematerialization may introduce before the pass backs off.

#### E. Placement Control (4 knobs)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 717 | `RematLowCostPlacementLimit` | DBL | Max placement distance for low-cost remat candidates |
| 720 | `RematMinDistance` | INT | Minimum def-to-remat distance (instructions) before remat is attempted |
| 721 | `RematPlacementLookback` | INT | Lookback window size for placement-site search |
| 725 | `RematSortRematChain` | INT | Sort remat chain by priority before placement (0=off, 1=on) |

These knobs control where rematerialized instructions are placed relative to their uses. `RematMinDistance` (720) ensures remat is not attempted for short live ranges where the original definition is close enough. `RematPlacementLookback` (721) limits how far back the placement algorithm scans when searching for a profitable insertion point.

#### F. Remat Budget (1 knob)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 704 | `RematBudget` | BDGT | Optimization budget for the late remat pass (phase 69) |

BDGT-typed knobs carry a primary value and a secondary counter. The budget is decremented as each remat decision is committed. When exhausted (secondary reaches zero), the pass stops processing further candidates. This provides a deterministic cap on compile-time cost.

#### G. SinkRemat (Phase 28) Knobs (8 knobs, indices 824--831)

| Index | Name | Type | Purpose |
|---|---|---|---|
| 824 | `SinkRematAbsCostLimit` | DBL | Absolute cost ceiling for sinking+remat decisions |
| 825 | `SinkRematBudget` | BDGT | Optimization budget for the sink+remat pass |
| 826 | `SinkRematDeltaRegsRatio` | DBL | Register pressure delta ratio threshold for sink profitability |
| 827 | `SinkRematEnable` | INT | Master enable for Phase 28 SinkRemat |
| 828 | `SinkRematMinDefPlaceDist` | INT | Minimum definition-to-placement distance for sinking |
| 829 | `SinkRematMinPlaceRefDist` | INT | Minimum placement-to-reference distance for sinking |
| 830 | `SinkRematMultiRefXBlkUsesPenaltyFactor` | DBL | Penalty multiplier for multi-reference cross-block uses |
| 831 | `SinkRematPredPenaltyFactor` | DBL | Penalty multiplier for sinking predicated instructions |

Phase 28's SinkRemat pass (entry: `sub_913A30`, core: `sub_A0F020`) sinks instructions closer to their uses and marks remat candidates. Knob 827 (`SinkRematEnable`) is the master switch. The distance knobs (828, 829) prevent unprofitable micro-sinks. The penalty factors (830, 831) make the cost model more conservative for predicated instructions and for instructions with multiple cross-block uses, where sinking may duplicate code along multiple paths.

#### Related Knob Outside the Remat Block

| Index | Name | Type | Purpose |
|---|---|---|---|
| 475 | `MovWeightForRemat` | DBL | MOV instruction weight in remat profitability scoring |

This knob sits in the general MOV-weight family (indices 474--476) rather than the Remat block. It tunes how MOV instructions contribute to the scheduling cost model's remat profitability calculation. When the remat candidate is a MOV chain, this weight determines the per-MOV cost used to decide whether rematerialization beats keeping the value live.

## DUMP_KNOBS_TO_FILE

The `DUMP_KNOBS_TO_FILE` environment variable triggers a full dump of all knob values to a file. Checked during `KnobInit` (`sub_7A0C10`) via `getenv("DUMP_KNOBS_TO_FILE")`:

```c
char* dump_path = getenv("DUMP_KNOBS_TO_FILE");
if (dump_path) {
    size_t len = strlen(dump_path);
    // Store into SSO string at knob_state+88..104
}
```

The path is stored in a small-string-optimized (SSO) buffer at knob_state offsets +88 through +104:

```
Offset  Size  Field
──────  ────  ─────────────────────────────────────
+88     8     data pointer (or first 8 inline bytes if len <= 15)
+96     8     string length
+104    8     capacity (or remaining inline bytes)
```

Paths of 15 bytes or fewer are stored inline without heap allocation. Longer paths allocate via the arena allocator at knob_state+8. The dump is produced later during compilation -- `KnobInit` only stores the path; the actual file write happens after all knobs are resolved.

This is the primary mechanism for discovering which knobs exist and what their current values are. Setting it produces a text file with all 1,294 knob names and their resolved values.

## Error Handling

The knob system uses structured error descriptors (96 bytes each) allocated from an arena:

```
Offset  Size  Field
──────  ────  ─────────────────────────────────────
+0      8     formatted message string pointer
+8      8     message length
+16     8     source file path pointer
+24     8     source file path length
+32     8     line number
+40     8     function name pointer
+48     48    (additional context fields)
```

Two error constructor functions:

| Function | Address | Purpose |
|---|---|---|
| `FormatKnobError` | `sub_79CDB0` | General knob error with `vsnprintf` formatting |
| `FormatKnobErrorWithContext` | `sub_79AED0` | Error with additional context (knob name, value) |
| `KnobError::Merge` | `sub_79A780` | Chains multiple errors for accumulated reporting |

Errors propagate through a tagged result: bit 0 of `*(result + 16)` is set on error, cleared on success. The `GetKnobIndex` return protocol:

```c
// Success:
*(byte*)(result + 16) &= ~1;    // clear error bit
*(int32*)(result) = knob_index;  // store index

// Failure:
*(byte*)(result + 16) |= 1;     // set error bit
*(result + 0..15) = error_desc;  // store error descriptor
```

## KnobValue Lifecycle

### Construction

`KnobValue::Destroy` (`sub_797790`) resets a 72-byte value slot before writing a new value. It switches on the type tag:

| Type | Destruction Action |
|---|---|
| 0-5, 7, 8 | No-op (POD types, no heap allocation) |
| 6 (int-list) | Walk doubly-linked list, free each node via `allocator+32` |
| 9 (opcode-list) | Walk doubly-linked list, free each node via `allocator+32` |
| 10 (int-list dynamic) | Free the growable array block |

### Deep Copy

`KnobValue::CopyFrom` (`sub_7978F0`) handles deep copy of value slots, switching on type to properly duplicate linked lists and allocated buffers.

`KnobInit` (`sub_7A0C10`) constructs a new knob state object by allocating `72 * count` bytes for the value array, then deep-copying each slot from a source state if one exists.

## Function Map

| Address | Size | Function | Confidence |
|---|---|---|---|
| `sub_6F04B0` | 6,824 | `ReportKnobError` (DAG) | HIGH |
| `sub_6F0820` | 2,782 | `GetKnobIndex` (DAG) | CERTAIN |
| `sub_6F0A30` | 8,700 | `RegisterKnob` (DAG) | HIGH |
| `sub_6F0FF0` | 13,000 | `GetKnobValue` (DAG) | HIGH |
| `sub_6F1B10` | 13,000 | `BuildKnobTable` (DAG) | HIGH |
| `sub_6F2380` | 14,000 | `ParseKnobString` (DAG) | HIGH |
| `sub_6F68C0` | 9,000 | `InitializeKnobs` (DAG) | HIGH |
| `sub_6F7360` | 18,306 | `ParseKnobValue` (DAG) | CERTAIN |
| `sub_6F83C0` | — | `ParseWhenShorthand` (DAG) | MEDIUM |
| `sub_797790` | 385 | `KnobValue::Destroy` | HIGH |
| `sub_7978F0` | 240 | `KnobValue::CopyFrom` | MEDIUM |
| `sub_7973E0` | 400 | `KnobType::GetSize` | MEDIUM |
| `sub_798280` | 900 | `ParsePhaseNameFragment` | MEDIUM |
| `sub_798B60` | 1,776 | `NamedPhases::ParsePhaseList` | CERTAIN |
| `sub_799250` | 68 | `IsPassDisabled` | HIGH |
| `sub_7992A0` | 894 | `IsPassDisabledFull` | HIGH |
| `sub_79A490` | 600 | `KnobError::AppendContext` | MEDIUM |
| `sub_79A5D0` | 800 | `KnobError::Format` | MEDIUM |
| `sub_79A780` | 2,200 | `KnobError::Merge` | MEDIUM |
| `sub_79AED0` | 1,000 | `FormatKnobErrorWithContext` | HIGH |
| `sub_79B240` | 518 | `GetKnobIndex` (OCG) | CERTAIN |
| `sub_79B450` | 200 | `GetKnobIndexWithValidation` | HIGH |
| `sub_79B530` | 3,296 | `ParseKnobsString` | HIGH |
| `sub_79C210` | 2,200 | `ParseKnobOverrides` | HIGH |
| `sub_79C9D0` | 1,600 | `KnobsInitFromEnv` | HIGH |
| `sub_79CDB0` | 1,400 | `FormatKnobError` | HIGH |
| `sub_79D070` | 2,312 | `ReadKnobsFile` | CERTAIN |
| `sub_79D990` | 7,073 | `KnobsInit` (master) | HIGH |
| `sub_79F540` | 3,640 | `ParseKnobValue` (OCG) | CERTAIN |
| `sub_7A0A90` | 350 | `KnobValue::CopyListValue` | MEDIUM |
| `sub_7A0C10` | 1,745 | `KnobInit` (per-knob) | HIGH |
| `sub_7A1B80` | 400 | `GetKnobIntValue` | MEDIUM |
| `sub_7A1CC0` | 350 | `GetKnobBoolValue` | MEDIUM |
| `sub_7A1E10` | 400 | `GetKnobStringValue` | MEDIUM |
| `sub_7A2860` | 2,100 | `SetKnobValue` | MEDIUM |
| `sub_7ACEA0` | 3,700 | `OCGKnobSetup` | MEDIUM |

## Reimplementation Notes

To reimplement the knobs system:

1. **Define the knob table** as a compile-time array of descriptors (name, alias, type). No need for ROT13 -- that is purely obfuscation. Use an enum for knob indices so call sites reference `KNOB_SchedNumBB_Limit` instead of magic index 294.

2. **Parse order matters.** Process sources in the documented priority order (env, file, CLI, pragma, WHEN). Last-write-wins semantics.

3. **The WHEN= system** is the complex part. You need FNV-1a hashing of function identifiers and a per-function override table. The hash table at `ctx+120 → +1128` uses open addressing with linear probing.

4. **Budget knobs** (OKT_BDGT) are just integers with a secondary tracking field. The secondary starts at 0 and is used by cost models to track how much "budget" remains during optimization.

5. **Int-range knobs** (OKT_IRNG) use `..` as the range separator: `"100..200"` means [100, 200]. Missing bounds default to `INT_MIN` (`0x80000000`) / `INT_MAX` (`0x7FFFFFFF`).

6. **The opcode-string-list type** (OKT_OPCODE_STR_LIST) carries pairs of (opcode_name, integer). The opcode name is resolved to an internal opcode ID via the SASS opcode table. Used for per-instruction tuning overrides.

## Cross-References

- [CLI Options](./cli-options.md) -- public command-line flags, the user-facing layer above knobs
- [Optimization Levels](./opt-levels.md) -- O-levels set specific knob presets
- [DUMPIR & NamedPhases](./dumpir.md) -- DUMPIR knob and phase-level dump control
- [Phase Manager](../passes/phase-manager.md) -- pass disable mechanism consumes the DisablePhases knob
- [Scheduling Algorithm](../scheduling/algorithm.md) -- consumes Sched* knobs
- [Allocator Architecture](../regalloc/overview.md) -- consumes RegAlloc* knobs
- [Mercury Encoder](../codegen/mercury.md) -- consumes Mercury* knobs and DAG knob table
