# Translation Unit Descriptor

The translation unit descriptor is the 424-byte structure at the heart of cudafe++'s multi-TU compilation support. Every source file processed by the frontend — whether via RDC separate compilation or C++20 module import — gets its own TU descriptor. The descriptor holds pointers to the parser state, scope stack snapshot, error context, and IL tree root for that translation unit. When the frontend switches from one TU to another, it saves the entire set of per-TU global variables into the outgoing descriptor's storage buffer and restores the incoming descriptor's saved values, making TU switching look like a cooperative context switch for compiler state.

The descriptor is allocated from the region-based arena (`sub_6BA0D0`), linked into a global TU chain, and managed through a TU stack that tracks the active-TU history for nested TU switches (e.g., when processing an entity requires switching to its owning TU temporarily).

## Key Facts

| Property | Value |
|---|---|
| Size | 424 bytes (confirmed by `print_trans_unit_statistics`: "translation units ... 424 bytes each") |
| Allocation | `sub_6BA0D0(424)` — region-based arena, never individually freed |
| Source file | `trans_unit.c` (EDG 6.6, address range `0x7A3A50`-`0x7A48B0`, ~12 functions) |
| Allocator | `sub_7A40A0` (`process_translation_unit`) |
| Save function | `sub_7A3A50` (`save_translation_unit_state`) |
| Restore function | `sub_7A3D60` (`switch_translation_unit`) |
| Fix-up function | `sub_7A3CF0` (`fix_up_translation_unit`) |
| Statistics | `sub_7A45A0` (`print_trans_unit_statistics`) |
| TU count global | `qword_12C7A78` (incremented on each allocation) |
| Active TU global | `qword_106BA10` (`current_translation_unit`) |
| Primary TU global | `qword_106B9F0` (`primary_translation_unit`) |

## Full Offset Map

The table below documents every field in the 424-byte TU descriptor. Offsets are byte positions from the start of the descriptor. Fields are identified from the initialization code in `process_translation_unit` (`sub_7A40A0`), the save/restore pair (`sub_7A3A50`/`sub_7A3D60`), and the fix-up function (`sub_7A3CF0`).

| Offset | Size | Field | Set By | Read By |
|---|---|---|---|---|
| `+0` | 8 | `next_tu` — linked list pointer to next TU in chain | `process_translation_unit` (via `qword_12C7A90`) | `fe_wrapup` TU iteration loop |
| `+8` | 8 | `prev_scope_state` — saved scope pointer (xmmword_126EB60+8) | `save_translation_unit_state` | `switch_translation_unit` |
| `+16` | 8 | `storage_buffer` — pointer to bulk registered-variable storage | `process_translation_unit` (allocates `sub_6BA0D0(per_tu_storage_size)`) | `save/switch_translation_unit` |
| `+24` | 160 | `file_scope_info` — file scope state block (20 qwords, initialized by `sub_7046E0`) | `sub_7046E0` (zeroes 20 fields at offsets 0-152 within this block) | Scope stack operations, `sub_704490` |
| `+184` | 8 | (cleared to 0) — within file scope info tail | `process_translation_unit` | — |
| `+192` | 8 | (cleared to 0) — gap between scope info and registered-variable zone | `process_translation_unit` | — |
| `+200` | 160 | **registered-variable direct fields** — zeroed bulk region (offsets +200 through +359) | `memset` in `process_translation_unit`; individual fields written by registered-variable initialization loop | `save/switch_translation_unit` via storage buffer |
| `+208` | 8 | `scope_stack_saved_1` — saved `qword_126EB70` (scope stack depth marker) | `save_translation_unit_state` (a1[26]) | `switch_translation_unit` |
| `+256` | 8 | `scope_stack_saved_2` — saved `qword_126EBA0` | `save_translation_unit_state` (a1[32]) | `switch_translation_unit` |
| `+320` | 8 | `scope_stack_saved_3` — saved `qword_126EBE0` | `save_translation_unit_state` (a1[40]) | `switch_translation_unit` |
| `+352` | 8 | (cleared to 0) — end of registered-variable zone | `process_translation_unit` | — |
| `+360` | 8 | (cleared to 0) — additional state word 1 | `process_translation_unit` | — |
| `+368` | 8 | (cleared to 0) — additional state word 2 | `process_translation_unit` | — |
| `+376` | 8 | `module_info_ptr` — pointer to module info structure (parameter `a3` of `process_translation_unit`) | `process_translation_unit` | Module import path, `a3[2]` back-link |
| `+384` | 8 | `il_state_ptr` — shortcut pointer for IL state (1344-byte aggregate at `unk_126E600`), set via registered-variable mechanism with `offset_in_tu = 384` | Registered-variable init loop | IL operations |
| `+392` | 2 | `flags` — bit field: byte 0 = `is_primary_tu` (1 if `a3 == NULL`), byte 1 = 0x01 (initialization sentinel, combined initial value = `0x0100`) | `process_translation_unit` | TU classification |
| `+394` | 14 | (padding / reserved) | — | — |
| `+408` | 4 | `error_severity_level` — copied from `dword_126EC90` (current maximum error severity) | `process_translation_unit` | Error reporting, recovery decisions |
| `+416` | 8 | (cleared to 0) — additional state | `process_translation_unit` | — |

### Layout Diagram

```text
Translation Unit Descriptor (424 bytes)
===========================================

 +0    [next_tu          ] -----> next TU in chain (NULL for last)
 +8    [prev_scope_state ] -----> saved scope ptr (from xmmword_126EB60+8)
+16    [storage_buffer   ] -----> heap block for registered variable values
+24    [                                                              ]
       [  file_scope_info (160 bytes, 20 qwords)                      ]
       [  initialized by sub_7046E0: all fields zeroed                ]
       [  scope state snapshot for this TU's file scope               ]
+184   [  (tail of scope info, cleared)                               ]
+192   [  (gap, cleared to 0)                                         ]
+200   [                                                              ]
       [  registered-variable direct fields (160 bytes, bulk zeroed)  ]
       [  includes scope stack snapshots at +208, +256, +320          ]
       [  individual fields set by registered-variable init loop      ]
+352   [  (cleared to 0)                                              ]
+360   [  (additional state, cleared)                                 ]
+368   [  (additional state, cleared)                                 ]
+376   [module_info_ptr   ] -----> module info (NULL for primary TU)
+384   [il_state_ptr      ] -----> shortcut to IL state in storage buffer
+392   [flags             ] 0x0100 initial; byte 0 = is_primary
+394   [  (reserved)      ]
+408   [error_severity    ] from dword_126EC90
+412   [  (pad)           ]
+416   [  (additional, 0) ]
+424   === end ===
```

### Initialization Sequence

The initialization in `process_translation_unit` proceeds in this order:

1. `[+0]` = 0 (next_tu pointer, not yet linked)
2. `[+16]` = `sub_6BA0D0(qword_12C7A98)` (allocate storage buffer, size = accumulated registered-variable total)
3. `[+8]` = 0 (prev_scope_state)
4. `sub_7046E0(tu + 24)` — zero-initialize the 160-byte file scope info block
5. `[+192]` = 0, `[+352]` = 0, `[+184]` = 0 — explicit clears around the bulk region
6. `memset(aligned(tu + 200), 0, ...)` — bulk-zero the registered-variable direct fields from +200 to +360 (aligned to 8-byte boundary)
7. `[+360]` = 0, `[+368]` = 0, `[+376]` = 0 — clear additional state
8. `[+392]` = `0x0100` (flags: initialized sentinel in high byte)
9. `[+408]` = 0, `[+416]` = 0
10. Registered-variable default-value loop: iterate `qword_12C7AA8` (registered variable list) and for each entry with `offset_in_tu != 0`, write `variable_address` into `tu_desc[offset_in_tu]`
11. `[+376]` = `a3` (module_info_ptr)
12. `[+392] byte 0` = `(a3 == NULL)` (is_primary flag)

## Lifecycle

### Phase 1: Registration (Before Any TU Processing)

Before the first translation unit is processed, every EDG subsystem registers its per-TU global variables by calling `f_register_trans_unit_variable` (`sub_7A3C00`). This happens during frontend initialization, before `dword_12C7A8C` (registration_complete) is set to 1.

The three core variables are registered by `register_builtin_trans_unit_variables` (`sub_7A4690`):

```c
// sub_7A4690 -- register_builtin_trans_unit_variables
f_register_trans_unit_variable(&dword_106BA08, 4, 0);   // is_recompilation
f_register_trans_unit_variable(&qword_106BA00, 8, 0);   // current_filename
f_register_trans_unit_variable(&dword_106B9F8, 4, 0);   // has_module_info
```

In total, approximately 217 calls to `f_register_trans_unit_variable` are made across all subsystems. Each call adds a 40-byte registration record to the linked list headed by `qword_12C7AA8` and accumulates the variable size into `qword_12C7A98` (the per-TU storage buffer size). The accumulated size determines how large the storage buffer allocation will be for each TU descriptor.

### Phase 2: Allocation and Initialization

When `process_translation_unit` (`sub_7A40A0`) is called for each source file:

```c
process_translation_unit(filename, is_recompilation, module_info_ptr)
```

1. If a current TU exists (`qword_106BA10 != 0`), save its state via `save_translation_unit_state`
2. Reset compilation state (`sub_5EAEC0` — error state reset)
3. If recompilation mode: reset parse state (`sub_585EE0`)
4. Set `dword_12C7A8C = 1` (registration complete — no more variable registrations allowed)
5. Allocate the 424-byte descriptor and its storage buffer
6. Initialize all fields (see sequence above)
7. Copy registered-variable defaults into the descriptor
8. Link into the TU chain

### Phase 3: Linking

The descriptor is linked into two structures simultaneously:

**TU Chain** (singly-linked list via `[+0]`):
- Head: `qword_106B9F0` (primary_translation_unit) — the first TU processed
- Tail: `qword_12C7A90` (tu_chain_tail) — the most recently allocated TU
- Linking: `*tu_chain_tail = new_tu; tu_chain_tail = new_tu`
- Used by: `fe_wrapup` to iterate all TUs during the 5-pass post-processing

**TU Stack** (singly-linked list of 16-byte stack entries):
- Top: `qword_106BA18` (translation_unit_stack_top)
- Each entry: `[+0]` = next, `[+8]` = tu_descriptor_ptr
- Free list: `qword_12C7AB8` (stack entries are recycled, not freed)
- Depth counter: `dword_106B9E8` (counts non-primary TUs on the stack)

```text
TU Chain:                    TU Stack:
                             qword_106BA18
primary_tu --> tu_2 --> tu_3    |
    ^                           v
    |                      [next|tu_3] --> [next|tu_2] --> [next|primary] --> NULL
qword_106B9F0                        each entry: 16 bytes
```

### Phase 4: Active TU Tracking

The global `qword_106BA10` always points to the currently active TU descriptor. All compiler state — parser globals, scope stack, symbol tables, error context — corresponds to this TU. Switching the active TU requires a full context switch through `switch_translation_unit`.

### Phase 5: Processing (5 Passes in fe_wrapup)

After parsing completes, `fe_wrapup` (`sub_588F90`) iterates the TU chain and performs 5 passes over all TUs:

1. **Pass 1** (`file_scope_il_wrapup`): per-TU scope cleanup, cross-TU entity marking
2. **Pass 2** (`set_needed_flags_at_end_of_file_scope`): compute needed-flags for entities
3. **Pass 3** (`mark_to_keep_in_il`): mark entities to keep in the IL tree
4. **Pass 4** (three sub-stages): clear unneeded instantiation flags, eliminate unneeded function bodies, eliminate unneeded IL entries
5. **Pass 5** (`file_scope_il_wrapup_part_3`): final cleanup, scope assertion, re-run of passes 2-4 for the primary TU

Each pass switches to the target TU via `switch_translation_unit` before processing.

### Phase 6: Pop and Cleanup

After `sub_588E90` (translation_unit_wrapup) and the main compilation passes complete, the TU is popped from the stack. The inline pop code in `process_translation_unit` (mirroring `pop_translation_unit_stack` at `sub_7A3F70`):

1. Assert: `stack_top->tu_ptr == current_tu` (stack integrity check)
2. Decrement `dword_106B9E8` if popped TU is not the primary TU
3. Move stack entry to free list (`qword_12C7AB8`)
4. If a previous TU remains on the stack, switch to it via `switch_translation_unit`

## Registered Variable Mechanism

The registered variable mechanism is the save/restore system that makes TU switching possible. It works in three phases: registration, save, and restore.

### Registration Phase

During frontend initialization, each subsystem calls `f_register_trans_unit_variable` to declare global variables that contain per-TU state. Each call creates a 40-byte registration record:

```text
Registered Variable Entry (40 bytes)
  [0]   8   next               linked list pointer
  [8]   8   variable_address   pointer to the global variable
  [16]  8   variable_size      number of bytes to save/restore
  [24]  8   offset_in_storage  byte offset within the TU storage buffer
  [32]  8   offset_in_tu       byte offset within the TU descriptor (0 = none)
```

Registration accumulates the total storage buffer size in `qword_12C7A98`. Each variable gets assigned a sequential offset within the buffer:

```c
// f_register_trans_unit_variable (sub_7A3C00), simplified
void f_register_trans_unit_variable(void *var_ptr, size_t size, size_t offset_in_tu) {
    assert(!registration_complete);   // dword_12C7A8C must be 0
    assert(var_ptr != NULL);

    record = alloc(40);
    record->next = NULL;
    record->variable_address = var_ptr;
    record->variable_size = size;
    record->offset_in_storage = per_tu_storage_size;  // qword_12C7A98
    record->offset_in_tu = offset_in_tu;

    // append to linked list
    if (!list_head) list_head = record;     // qword_12C7AA8
    if (list_tail) list_tail->next = record;
    list_tail = record;                      // qword_12C7AA0

    // align size to 8 bytes, accumulate
    size_t aligned = (size + 7) & ~7;
    per_tu_storage_size += aligned;          // qword_12C7A98
}
```

The third parameter `offset_in_tu` is non-zero only for variables that need a direct shortcut pointer within the TU descriptor itself. For example, the 1344-byte IL state aggregate at `unk_126E600` registers with `offset_in_tu = 384`, so `tu_desc[384]` receives a pointer to the stored copy of that aggregate within the storage buffer. Most variables pass 0 (no shortcut needed).

### Save Phase (save_translation_unit_state)

When switching away from a TU, `sub_7A3A50` saves the current state:

```c
// save_translation_unit_state (sub_7A3A50), simplified
void save_translation_unit_state(tu_desc *tu) {
    char *storage = tu->storage_buffer;     // tu[2]

    // Iterate all registered variables
    for (reg = registered_variable_list_head; reg; reg = reg->next) {
        // Copy current global value into storage buffer
        void *dst = storage + reg->offset_in_storage;
        memcpy(dst, reg->variable_address, reg->variable_size);

        // If this variable has a direct field in the TU descriptor,
        // store a pointer to the saved copy there
        if (reg->offset_in_tu != 0) {
            *(void **)((char *)tu + reg->offset_in_tu) = dst;
        }
    }

    // Save scope stack state (3 explicit fields)
    tu->scope_saved_1 = qword_126EB70;   // tu[26]
    tu->scope_saved_2 = qword_126EBA0;   // tu[32]
    tu->scope_saved_3 = qword_126EBE0;   // tu[40]

    // Save file scope indices via sub_704490
    if (dword_126C5E4 != -1) {
        sub_704490(dword_126C5E4, 0, 0);
        // Walk scope stack entries, clear scope-to-TU back-pointers
        for (entry = scope_top; entry; entry -= 784) {
            if (entry[+192])
                *(int *)(entry[+192] + 240) = -1;
            if (!entry[+4]) break;
        }
    }
}
```

### Restore Phase (switch_translation_unit)

When switching to a different TU, `sub_7A3D60` restores its state:

```c
// switch_translation_unit (sub_7A3D60), simplified
void switch_translation_unit(tu_desc *target) {
    assert(current_tu != NULL);       // qword_106BA10

    if (current_tu == target) return; // no-op if already active

    save_translation_unit_state(current_tu);  // save outgoing
    current_tu = target;              // qword_106BA10 = target

    char *storage = target->storage_buffer;

    // Iterate all registered variables -- REVERSE of save
    for (reg = registered_variable_list_head; reg; reg = reg->next) {
        // Copy saved value from storage buffer back to global
        memcpy(reg->variable_address, storage + reg->offset_in_storage,
               reg->variable_size);

        // Update shortcut pointer if present
        if (reg->offset_in_tu != 0) {
            *(void **)((char *)target + reg->offset_in_tu) =
                memcpy result;  // points into the global
        }
    }

    // Restore scope stack state
    xmmword_126EB60_high = target[1];  // prev_scope_state
    qword_126EB70 = target[26];
    qword_126EBA0 = target[32];
    qword_126EBE0 = target[40];

    // Rebuild file scope indices via sub_704490
    if (dword_126C5E4 != -1) {
        // Recompute scope-to-TU back-pointers
        for (entry = scope_top; entry; entry -= 784) {
            if (entry[+192])
                *(int *)(entry[+192] + 240) = index_formula;
            if (!entry[+4]) break;
        }
        sub_704490(dword_126C5E4, 1, computed_flag);
    }
}
```

The key asymmetry between save and restore: `memcpy` direction is reversed. Save copies `global -> storage_buffer`. Restore copies `storage_buffer -> global`. The shortcut pointer (`offset_in_tu`) semantics also differ: during save it points into the storage buffer; during restore it points back to the global variable.

### Fix-Up Phase

After the primary TU's registered-variable defaults are first copied into its descriptor, `fix_up_translation_unit` (`sub_7A3CF0`) performs a one-time pass that writes variable-address pointers into the TU descriptor's shortcut fields:

```c
// fix_up_translation_unit (sub_7A3CF0)
void fix_up_translation_unit(tu_desc *primary) {
    assert(primary->next_tu == NULL);  // must be the first TU

    for (reg = registered_variable_list_head; reg; reg = reg->next) {
        if (reg->offset_in_tu != 0) {
            *(void **)((char *)primary + reg->offset_in_tu) =
                reg->variable_address;
        }
    }
}
```

This ensures the primary TU's shortcut pointers point directly to the live global variables rather than the storage buffer, since the primary TU's globals are the "real" values (not copies).

## TU Stack Operations

The TU stack supports nested TU switches. This is needed when processing an entity declared in a different TU requires temporarily switching to that TU's context.

### Push (push_translation_unit_stack)

```c
// sub_7A3EF0 -- push_translation_unit_stack
void push_translation_unit_stack(tu_desc *tu) {
    // Allocate stack entry from free list or fresh allocation
    stack_entry *entry;
    if (stack_entry_free_list) {          // qword_12C7AB8
        entry = stack_entry_free_list;
        stack_entry_free_list = entry->next;
    } else {
        entry = alloc(16);               // sub_6B7340(16)
        ++stack_entry_count;              // qword_12C7A80
    }

    entry->tu_ptr = tu;                  // [+8]
    entry->next = tu_stack_top;          // [+0] = qword_106BA18

    // If pushing a different TU than current, switch to it
    if (current_tu != tu)
        switch_translation_unit(tu);

    // Track depth for non-primary TUs
    if (tu != primary_tu)
        ++tu_stack_depth;                // dword_106B9E8

    tu_stack_top = entry;                // qword_106BA18
}
```

### Pop (pop_translation_unit_stack)

```c
// sub_7A3F70 -- pop_translation_unit_stack
void pop_translation_unit_stack() {
    stack_entry *top = tu_stack_top;      // qword_106BA18

    // Integrity assertion: top-of-stack TU must match current TU
    assert(top->tu_ptr == current_tu);    // top[+8] == qword_106BA10

    if (top->tu_ptr != primary_tu)
        --tu_stack_depth;                 // dword_106B9E8

    // Pop: move top to free list, advance stack
    stack_entry *prev = top->next;
    top->next = stack_entry_free_list;   // return to free list
    stack_entry_free_list = top;
    tu_stack_top = prev;                 // qword_106BA18

    // If another TU remains, switch to it
    if (prev)
        switch_translation_unit(prev->tu_ptr);
}
```

### Push Entity's TU (push_entity_translation_unit)

A convenience function `sub_7A3FE0` pushes the TU that owns a given entity:

```c
// sub_7A3FE0 -- push_entity_translation_unit
int push_entity_translation_unit(entity *ent) {
    if (ent->flags_81 & 0x20)  return 0;  // anonymous entity, no TU
    tu_desc *owning_tu = get_entity_tu(ent);  // sub_741960
    if (owning_tu == current_tu)  return 0;   // already in correct TU

    push_translation_unit_stack(owning_tu);
    return 1;  // caller must pop when done
}
```

## TU Stack Entry Layout

```text
TU Stack Entry (16 bytes)
  [0]   8   next            next entry in stack (toward bottom) or free list
  [8]   8   tu_desc_ptr     pointer to the TU descriptor
```

Stack entries are recycled through a free list (`qword_12C7AB8`). They are allocated by `sub_6B7340` (general storage, not arena) on first use and never deallocated — only returned to the free list on pop.

## TU Correspondence (24 bytes)

When processing multiple TUs in RDC mode, the frontend must track structural equivalence between types and declarations across TUs. Each correspondence is a 24-byte node:

```text
Trans Unit Correspondence (24 bytes)
  [0]   8   next            linked list pointer
  [8]   8   ptr             pointer to the corresponding entity/type
  [16]  4   refcount        reference count (freed when decremented to 1)
  [20]  1   flag            correspondence type flag
```

Allocation uses a free list (`qword_12C7AB0`) with fallback to arena allocation (`sub_6BA0D0(24)`). The reference-counted deallocation in `free_trans_unit_corresp` (`sub_7A3BB0`) asserts that `refcount > 0` before decrementing, and only pushes the node onto the free list when the count reaches 1 (not 0 — the last reference is the free-list entry itself).

## Global Variables

### TU State

| Global | Type | Identity |
|---|---|---|
| `qword_106BA10` | `tu_desc*` | `current_translation_unit` — always points to the active TU |
| `qword_106B9F0` | `tu_desc*` | `primary_translation_unit` — the first TU processed (head of chain) |
| `qword_12C7A90` | `tu_desc*` | `tu_chain_tail` — last TU in the linked list |
| `qword_106BA18` | `stack_entry*` | `translation_unit_stack_top` — top of the TU stack |
| `dword_106B9E8` | `int32` | `tu_stack_depth` — number of non-primary TUs on the stack |
| `qword_106BA00` | `char*` | `current_filename` — source file name for the active TU |
| `dword_106BA08` | `int32` | `is_recompilation` — 1 if this TU is being recompiled |
| `dword_106B9F8` | `int32` | `has_module_info` — 1 if the active TU has module info |

### Registration Infrastructure

| Global | Type | Identity |
|---|---|---|
| `qword_12C7AA8` | `reg_entry*` | `registered_variable_list_head` |
| `qword_12C7AA0` | `reg_entry*` | `registered_variable_list_tail` |
| `qword_12C7A98` | `size_t` | `per_tu_storage_size` — accumulated total size of all registered variables (determines storage buffer allocation) |
| `dword_12C7A8C` | `int32` | `registration_complete` — set to 1 when first TU is allocated; guards against late registration |
| `dword_12C7A88` | `int32` | `has_seen_module_tu` — set to 1 when a TU with module info is processed |

### Free Lists and Counters

| Global | Type | Identity |
|---|---|---|
| `qword_12C7AB8` | `stack_entry*` | `stack_entry_free_list` |
| `qword_12C7AB0` | `corresp*` | `corresp_free_list` |
| `qword_12C7A78` | `int64` | `tu_count` — total TU descriptors allocated |
| `qword_12C7A80` | `int64` | `stack_entry_count` — total stack entries allocated (not freed) |
| `qword_12C7A68` | `int64` | `registration_count` — total registered variable entries |
| `qword_12C7A70` | `int64` | `corresp_count` — total correspondence nodes allocated |

### Correspondence State

| Global | Type | Identity |
|---|---|---|
| `dword_106B9E4` | `int32` | Correspondence state variable 1 (per-TU, registered) |
| `dword_106B9E0` | `int32` | Correspondence state variable 2 (per-TU, registered) |
| `qword_12C7798` | `int64` | Correspondence state variable 3 (per-TU, registered) |
| `qword_12C7800` | `[14]` | Correspondence hash table 1 (0x70 bytes) |
| `qword_12C7880` | `[14]` | Correspondence hash table 2 (0x70 bytes) |
| `qword_12C7900` | `[14]` | Correspondence hash table 3 (0x70 bytes) |

## Reset Functions

Two reset functions exist for different scopes:

**`reset_translation_unit_state`** (`sub_7A4860`) — zeroes the 6 core runtime globals. Called during error recovery or frontend teardown:

```c
qword_106BA10 = 0;  // current_tu
qword_106B9F0 = 0;  // primary_tu
qword_12C7A90 = 0;  // tu_chain_tail
dword_106B9F8 = 0;  // has_module_info
qword_106BA18 = 0;  // tu_stack_top
dword_106B9E8 = 0;  // tu_stack_depth
```

**`init_translation_unit_tracking`** (`sub_7A48B0`) — zeroes all 13 tracking globals. Called during frontend initialization before any registrations:

```c
qword_12C7AA8 = 0;  // registered_variable_list_head
qword_12C7AA0 = 0;  // registered_variable_list_tail
qword_12C7A98 = 0;  // per_tu_storage_size
dword_106BA08 = 0;  // is_recompilation
qword_106BA00 = 0;  // current_filename
qword_12C7AB8 = 0;  // stack_entry_free_list
qword_12C7AB0 = 0;  // corresp_free_list
qword_12C7A80 = 0;  // stack_entry_count
qword_12C7A78 = 0;  // tu_count
qword_12C7A68 = 0;  // registration_count
qword_12C7A70 = 0;  // corresp_count
dword_12C7A8C = 0;  // registration_complete
dword_12C7A88 = 0;  // has_seen_module_tu
```

## Memory Statistics

The `print_trans_unit_statistics` function (`sub_7A45A0`) reports the allocation counts and total memory for the four structure types managed by the TU system:

| Structure | Size | Counter | Storage |
|---|---|---|---|
| Trans unit correspondence | 24 bytes | `qword_12C7A70` | Arena |
| Translation unit descriptor | 424 bytes | `qword_12C7A78` | Arena (gen. storage) |
| TU stack entry | 16 bytes | `qword_12C7A80` | General storage |
| Variable registration | 40 bytes | `qword_12C7A68` | General storage |

## Function Map

| Address | Identity | Confidence | Role |
|---|---|---|---|
| `sub_7A3A50` | `save_translation_unit_state` | HIGH | Save all per-TU globals to storage buffer |
| `sub_7A3B50` | `alloc_trans_unit_corresp` | HIGH | Allocate 24-byte correspondence node |
| `sub_7A3BB0` | `free_trans_unit_corresp` | HIGH | Reference-counted deallocation |
| `sub_7A3C00` | `f_register_trans_unit_variable` | DEFINITE | Register a global for per-TU save/restore |
| `sub_7A3CF0` | `fix_up_translation_unit` | DEFINITE | One-time shortcut pointer fix-up for primary TU |
| `sub_7A3D60` | `switch_translation_unit` | DEFINITE | Context-switch to a different TU |
| `sub_7A3EF0` | `push_translation_unit_stack` | HIGH | Push TU onto the stack |
| `sub_7A3F70` | `pop_translation_unit_stack` | DEFINITE | Pop TU from the stack |
| `sub_7A3FE0` | `push_entity_translation_unit` | MEDIUM-HIGH | Push the TU that owns a given entity |
| `sub_7A40A0` | `process_translation_unit` | DEFINITE | Main entry: allocate, init, parse, cleanup |
| `sub_7A45A0` | `print_trans_unit_statistics` | HIGH | Memory usage report for TU subsystem |
| `sub_7A4690` | `register_builtin_trans_unit_variables` | HIGH | Register the 3 core per-TU globals |
| `sub_7A4860` | `reset_translation_unit_state` | DEFINITE | Zero 6 runtime globals |
| `sub_7A48B0` | `init_translation_unit_tracking` | DEFINITE | Zero all 13 tracking globals |

## Assertions

The TU system contains 8 assertion sites (calls to `sub_4F2930` with source path `trans_unit.c`):

| Line | Function | Condition | Meaning |
|---|---|---|---|
| 163 | `free_trans_unit_corresp` | `refcount > 0` | Correspondence double-free |
| 227 | `f_register_trans_unit_variable` | `!registration_complete` | Variable registered after first TU allocated |
| 230 | `f_register_trans_unit_variable` | `var_ptr != NULL` | NULL pointer passed for registration |
| 469 | `fix_up_translation_unit` | `primary_tu->next == NULL` | Fix-up called on non-primary TU |
| 514 | `switch_translation_unit` | `current_tu != NULL` | Switch attempted with no active TU |
| 556 | `pop_translation_unit_stack` | `stack_top->tu == current_tu` | Stack/active TU mismatch |
| 696 | `process_translation_unit` | `!(a3==NULL && has_seen_module)` | Non-module TU after module TU |
| 725 | `process_translation_unit` | `is_recompilation` (when primary and first TU) | Primary TU must be a recompilation |

## Cross-References

- [RDC Mode](../cuda/rdc-mode.md) — multi-TU compilation: correspondence system, cross-TU IL copying, module ID generation
- [Frontend Wrapup](../pipeline/fe-wrapup.md) — the 5-pass post-processing architecture that iterates the TU chain
- [Scope Entry](scope-entry.md) — 784-byte scope stack entries saved/restored during TU switches
- [Entity Node](entity-node.md) — entities carry a back-pointer to their owning TU (extracted via `sub_741960`)
- [IL Overview](../il/overview.md) — the IL tree rooted in each TU's file scope
- [Pipeline Overview](../pipeline/overview.md) — where `process_translation_unit` sits in the full pipeline
