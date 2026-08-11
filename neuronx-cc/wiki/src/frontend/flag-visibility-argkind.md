# Flag Visibility Taxonomy (ArgKind)

> *All symbols, addresses, and string literals on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython `.so` modules; cp311/cp312 share the same layout). The visibility machinery lives in `neuronxcc/driver/Arguments.cpython-310-x86_64-linux-gnu.so` and the custom `argparse` actions in `neuronxcc/driver/Actions.cpython-310-x86_64-linux-gnu.so`. Offsets are `.text` offsets within each `.so`; treat every offset as version-pinned.*

## Abstract

Every `--flag` the driver registers is classified into a visibility tier, and the tier decides which `--help` surface — if any — the flag appears on. The classification is an enum, `ArgKind`, and the surfacing is done by three help paths: ordinary `--help`, `--help-hidden`, and `--help-hidden-list`. A reimplementer who treats `--help` output as the flag catalog will see roughly the public face only; the hidden and internal tiers are deliberately suppressed from the default help and recovered through the two extra help actions. This page recovers the full `ArgKind` member set, the *single* automatic classification rule (the `internal-` name prefix), and the help-filtering logic that maps each tier to a surface.

`ArgKind` is a stdlib `enum.Enum` defined in `Arguments.so` (the `enum`/`Enum` identifiers are interned in the module). Its members, read directly from the `.rodata` symbol pool, are **`PUBLIC`, `HIDDEN`, `INTERNAL`, `EARG`, `Harg`** — five tokens, not the three a casual read of the `_ArgumentRegistry` record suggests. The classification is driven by `_AddArgumentCallInterceptor.add_argument`: it tests the flag name with `name.startswith("internal-")` against the interned literal `"internal-"`, and on a match stamps `ArgKind.INTERNAL`. There is no other *automatic* rule — `HIDDEN`, `EARG`, and `Harg` are assigned by an explicit `kind=` keyword at the `add_argument` call site, never inferred from the name. Each registration is mirrored into `_ArgumentRegistry` as the 4-field record `argument,kind,job,default`, the literal that appears verbatim in `.rodata`.

The bool/default *semantics* the flags carry — `--no-X` negation pairs, `--enable-X`/`--disable-X` pairs, deprecation warnings, list-extend, and `true/false` string coercion — are not argparse built-ins; they are a small library of custom `argparse.Action` subclasses in `Actions.so` (`NoableArgumentAction`, `EnableDisableArgumentAction`, `Deprecated*ArgumentAction`, `ExtendAction`, `SetArgumentsAction`, `RecordUsedAction`, the three `Help*Action`s) plus the module-level `str2bool` helper. This page documents the visibility tiers and each of those actions, naming the real symbols.

For reimplementation, the contract is:

- **The `ArgKind` enum** — the five members `{PUBLIC, HIDDEN, INTERNAL, EARG, Harg}` and the fact that only the first three are fully wired as live enum identifiers (with a correction note on `Harg`).
- **The one automatic rule** — `name.startswith("internal-")` → `INTERNAL`, applied in `_AddArgumentCallInterceptor.add_argument`; everything else defaults `PUBLIC` unless a `kind=` kwarg overrides.
- **The three help surfaces** — `--help` (PUBLIC only), `--help-hidden` (`print_help_hidden`), `--help-hidden-list` (`print_help_hidden_list`, the INTERNAL registry dump) — and the table format `"%-15s %-8s --%s: %s"`.
- **The custom Action library** — what each action does to `option_strings`, defaults, and the parsed value, so the bool/negation/deprecation/extend semantics can be rebuilt.

| | |
|---|---|
| **Visibility module** | `neuronxcc/driver/Arguments…so` (823,192 B) |
| **Enum** | `ArgKind(enum.Enum)` — members `PUBLIC, HIDDEN, INTERNAL, EARG, Harg` |
| **Classification site** | `_AddArgumentCallInterceptor.add_argument` @ `0x19210` (size `0x1bcf`) |
| **Auto rule** | `name.startswith("internal-")` (literal `"internal-"`) → `ArgKind.INTERNAL` |
| **Registry record** | `argument,kind,job,default` → `_ArgumentRegistry.arguments_by_context` (`OrderedDict`) |
| **Help (public)** | argparse default — PUBLIC only; suppressed flags use `argparse.SUPPRESS` |
| **`--help-hidden`** | `InterceptingArgumentParser.print_help_hidden` @ `0x1cc90` |
| **`--help-hidden-list`** | `InterceptingArgumentParser.print_help_hidden_list` @ `0x17150` (INTERNAL dump) |
| **Help table format** | `"%-15s %-8s --%s: %s"` (group/dest · kind · flag · help) |
| **Missing-help placeholder** | `"MISSING HELP"` |
| **Actions module** | `neuronxcc/driver/Actions…so` — 10 Action classes + `str2bool` |

---

## The `ArgKind` Enum

### Purpose

`ArgKind` is the single classification axis for every driver flag. It is a stdlib `enum.Enum` (`Arguments.so` interns both `enum` and `Enum`, and `ArgKind` itself as `__pyx_n_s_ArgKind`). The value tags every flag at registration time and is stored in the `kind` column of the `_ArgumentRegistry` record, where the help-filtering paths read it back.

### Members

All five member tokens are present in the `Arguments.so` `.rodata` string pool. They are not equally wired, and the symbol *form* of each token is the evidence for how live it is:

| Member | Symbol form in `.rodata` | Confidence | Role |
|---|---|---|---|
| `PUBLIC` | `__pyx_n_s_PUBLIC` (interned name) | CERTAIN | Default tier; shown by ordinary `--help` |
| `HIDDEN` | `__pyx_n_s_HIDDEN` (interned name) | CERTAIN | Hand-tagged debug flags; shown by `--help-hidden` |
| `INTERNAL` | `__pyx_n_s_INTERNAL` (interned name) | CERTAIN | Auto-assigned by `internal-` prefix; dumped by `--help-hidden-list` |
| `EARG` | `__pyx_n_u_EARG` (interned **unicode** value) | HIGH | Experimental ArgKind; help-suppressed, listed by `--help-hidden` |
| `Harg` | bare string only — **no** `__pyx_n_s/n_u/k_Harg` | LOW | Help-suppressed sub-kind; never user-facing |

> **NOTE —** two different member counts for `ArgKind` are both defensible, depending on what counts as a member. Three tokens — `PUBLIC`, `HIDDEN`, `INTERNAL` — survive as interned *name* symbols (`__pyx_n_s_*`, the form Cython emits when an enum member is referenced by identifier), and that is the set [The Two-Parser Architecture](two-parser-architecture.md) and the `_ArgumentRegistry` record description use. The enum string pool in this build carries two more, `EARG` and `Harg`. This page uses the five-token pool set and marks how live each one is.

> **NOTE —** the symbol-form distinction is the whole basis of the `EARG`/`Harg` confidence split. `PUBLIC`/`HIDDEN`/`INTERNAL` appear as `__pyx_n_s_*` (interned *name* strings — what Cython emits when an enum member is referenced by identifier, e.g. `ArgKind.INTERNAL`). `EARG` appears as `__pyx_n_u_EARG`, an interned *unicode value* — consistent with being an enum member whose value is the string `"EARG"`, or with being compared as a string literal. `Harg` appears as a *bare* string at one `.rodata` offset with **no** interned-symbol wrapper and **no** occurrence anywhere else in the driver tree (`rg -l --binary 'Harg'` over `Arguments.so` + the `commands/` dir returns only that one `Arguments.so` hit). `Harg` is therefore tagged LOW: present in the pool, but not provably a live, code-referenced member in this build. Treat it as a help-suppressed sub-kind whose practical effect (never appear in any help surface) is what the taxonomy ascribes to it; do not assert it is reached at runtime.

### Stability ordering

Visibility tiers also encode a *stability* contract — how likely a flag is to change or vanish between releases. Ordered most- to least-stable:

```text
PUBLIC            no prefix; documented user surface; stable
  > HIDDEN        hand-tagged debug knobs; undocumented but supported
  > --enable-internal-*   in-development feature toggles (off by default)
  > --internal-*          raw developer knobs (auto-INTERNAL by prefix)
  > --enable-experimental-* / EARG   bleeding-edge paths
  > *unsafe* experimental            semantics-changing, risk-flagged
Harg-tagged args are never user-facing on any surface.
```

The three naming *families* map onto these tiers by name convention, and only one of the mappings is automatic (see [Classification](#classification--the-only-automatic-rule)):

| Family | Example | Resolved kind | Surface |
|---|---|---|---|
| `--internal-<name>` | `--internal-hlo-remat` | `INTERNAL` (auto, prefix) | `--help-hidden-list` |
| `--enable-internal-<name>` | `--enable-internal-new-backend` | `INTERNAL` (substring `internal-`) | `--help-hidden-list` |
| `--enable-experimental-<name>` | `--enable-experimental-spmd` | `EARG` (explicit `kind=`) | `--help-hidden` |

> **GOTCHA —** `--enable-internal-X` resolves to `INTERNAL` because the prefix test is a `startswith("internal-")` on the *dest-form* name after the leading `--`/`enable-` has been normalized away — the substring `internal-` still leads the compared token. A reimplementer who tests the raw `--enable-internal-X` option-string with `startswith("internal-")` will get a *false* and mis-classify the flag as `PUBLIC`. The compared value is the normalized name, not the option string.

---

## Classification — the only automatic rule

### Entry Point

```text
InterceptingArgumentParser.add_argument            @0x11320   ── driver-side add_argument
  └─ _AddArgumentCallInterceptor.add_argument      @0x19210   ── the interception core (size 0x1bcf)
        ├─ name.startswith("internal-")            literal "internal-"  ── the ONE auto rule
        ├─ pop kwargs: kind (ArgKind), job                     ── explicit overrides
        ├─ super().add_argument(*args, **kwargs)               ── real argparse registration
        └─ _ArgumentRegistry.storeArgument(ctx, action, kind, job, default)  @0x103d0
```

### Algorithm

The interceptor is the proxy that both `InterceptingArgumentParser` and any group/mutually-exclusive-group return, so flags added to a *group* are intercepted too (plain argparse bypasses the parser for `group.add_argument`). It performs the classification, then delegates to real argparse, then mirrors the result into the registry.

```c
function AddArgumentCallInterceptor_add_argument(self, *args, **kwargs):   // @0x19210
    // args[0] is the primary option string, e.g. "--internal-hlo-remat";
    // the leading "--" is stripped to form the compared name (see B.4 of the
    // registry model). The Cython literal compared is __pyx_kp_u_internal = "internal-".
    name = normalize(args[0])                     // strip leading "--"

    // ── the SINGLE automatic classification rule ──────────────────────────
    if name.startswith("internal-"):              // literal "internal-" @ pool
        kind = ArgKind.INTERNAL                   // auto-INTERNAL by name prefix
    else:
        kind = kwargs.pop("kind", ArgKind.PUBLIC) // explicit kind= overrides; else PUBLIC
    job = kwargs.pop("job", None)                 // owning pipeline Job association

    // ── real argparse registration ───────────────────────────────────────
    action = super().add_argument(*args, **kwargs)   // @0x11320 → argparse

    // ── read back the default and mirror into the side registry ──────────
    default = action.default                      // argparse-captured default (get_default)
    help    = action.help or "MISSING HELP"       // placeholder literal "MISSING HELP"
    self.registry.storeArgument(self.context, action, kind, job, default)  // @0x103d0
    return action
```

> **QUIRK —** the `internal-` test is a *prefix* test on the normalized name, but it catches both `--internal-X` and `--enable-internal-X` because the normalized dest-form of the latter still leads with `internal-` (the `enable-` is consumed elsewhere by the `EnableDisableArgumentAction`/dest mangling). This is why the two visibly different families collapse to the same `INTERNAL` kind without two separate rules — a single `startswith("internal-")` covers both. The rule itself is read from the literal and the `startswith` call; the precise normalization step that strips `enable-` is deduced from the family-to-kind mapping rather than traced [INFERRED].

> **NOTE —** the default tier is `PUBLIC`, and there is **no** automatic rule for `HIDDEN`, `EARG`, or `Harg`. Those three are stamped by an explicit `kind=` keyword at the developer's `add_argument` call. So the per-flag `HIDDEN`/`EARG` byte is *not* statically recoverable from the flag name alone; it is set inline at each registration inside `CompileCommand.__init__`. This is the residual uncertainty behind any per-flag `PUB/HID/INT/EARG` tag in the [flag catalog](flag-catalog.md): the prefix rule and the help-string presence are firm, but the exact explicit-kind byte is buried in the deeply-nested `__init__` decompilation.

### The registry record

Every classification is persisted into `_ArgumentRegistry` via `storeArgument` (@`0x103d0`), keyed by a *context* label so group-added flags land under the right context:

```c
function ArgumentRegistry_storeArgument(self, context, argument, kind, job, default):  // @0x103d0
    // record fields = the 4-field literal "argument,kind,job,default," in .rodata
    rec = record(argument, kind, job, default)
    self.arguments_by_context.setdefault(context, []).append(rec)  // OrderedDict, per-context list
```

The 4-field record literal `argument,kind,job,default,` and `arguments_by_context` are both verbatim `.rodata` strings. This is the data the help paths walk.

---

## Help Surfaces — how each tier renders

### Purpose

There are exactly three help surfaces, and the `kind` column decides which one a flag reaches. Public flags are the argparse default; hidden and internal flags register their `help=` as `argparse.SUPPRESS` (the `SUPPRESS` token is interned, `__pyx_n_s_SUPPRESS`), so they vanish from ordinary `--help` and are recovered only by the two custom help methods.

### The three paths

| Invocation | Method | Symbol | Filters by | Renders |
|---|---|---|---|---|
| `--help` | argparse default `format_help` | (stdlib) | `kind == PUBLIC` (SUPPRESS hides the rest) | normal help block |
| `--help-hidden` | `print_help_hidden` | `@0x1cc90` | the `HIDDEN` set (+ help-suppressed `EARG`) | hidden listing |
| `--help-hidden-list` | `print_help_hidden_list` | `@0x17150` | `kind == ArgKind.INTERNAL` | INTERNAL dump **with defaults** |

The two custom methods are driven by the `Help*Action` classes registered as the `--help-hidden` / `--help-hidden-list` flags (cf. [Actions](#custom-action-library)); each walks `arguments_by_context` and prints with one fixed table format.

### Algorithm

```c
function InterceptingArgumentParser_print_help_hidden(self):        // @0x1cc90
    print(self.format_usage())                                      // reuse argparse usage header
    for context, records in self.registry.arguments_by_context:
        for rec in records:
            if rec.kind == ArgKind.HIDDEN or rec.kind == ArgKind.EARG:  // hidden + experimental
                emit(rec)                                           // format below
    // EARG (experimental) flags are help-suppressed from --help but listed here.

function InterceptingArgumentParser_print_help_hidden_list(self):   // @0x17150
    for context, records in self.registry.arguments_by_context:
        for rec in records:
            if rec.kind == ArgKind.INTERNAL:        // RichCompare vs ArgKind.INTERNAL (~line 1051)
                emit_with_default(rec)              // INTERNAL flags + their default values

function emit(rec):
    // table literal "%-15s %-8s --%s: %s"  — group/dest | kind | flag | help
    help = rec.help or "MISSING HELP"
    printf("%-15s %-8s --%s: %s", rec.group_or_dest, rec.kind, rec.flag, help)
```

> **QUIRK —** `--help-hidden-list` is the *internal registry dump*, not a second hidden-help view. It filters strictly on `kind == ArgKind.INTERNAL` and emits each flag **with its captured default value** — it is the introspection path that lets a developer enumerate every `--internal-*` / `--enable-internal-*` knob and its default in one shot. `--help-hidden` (no `-list`) is the broader hidden/experimental view (`HIDDEN` + `EARG`) and does *not* specially print defaults. A reimplementer who collapses the two into one method loses the INTERNAL-only, default-printing semantics of the `-list` variant.

> **GOTCHA —** flags carry `help=argparse.SUPPRESS` to vanish from `--help`, but their `help` *string* is still stored in the registry for the hidden paths. When a hidden/internal flag has no help text at all, the registry substitutes the literal `"MISSING HELP"` rather than printing an empty cell. A reimplementer that keys "is this flag hidden?" off an empty help string will mis-handle the `MISSING HELP` placeholder.

---

## Custom Action Library

### Purpose

The bool, negation, deprecation, list-extend, and `true/false`-coercion semantics that the flag catalog relies on are implemented as custom `argparse.Action` subclasses in `Actions.so`, plus the module-level `str2bool`. These are the driver-lane equivalents of the Penguin backend's `addBoolOption` ([3.2](two-parser-architecture.md)), but for the user-facing CLI. Every class is present as a live qualname in the module symbol table (not stripped).

### Action map

| Class | Symbols | Confidence | Behavior |
|---|---|---|---|
| `NoableArgumentAction` | `__init__`, `__call__` | CERTAIN | For `--X`, builds `new_option_strings` adding a `--no-`/`--no` negation; default from action `default`/`const` |
| `EnableDisableArgumentAction` | `__init__`, `__call__` | CERTAIN | Pairs `--enable-X` / `--disable-X` (literals `--enable`, `--disable`) |
| `DeprecatedArgumentAction` | `__init__`, `__call__` | CERTAIN | Accepts the flag but warns: `"<flag> is deprecated and will be removed in a future release."` |
| `DeprecatedStoreTrueArgumentAction` | `__init__`, `__call__` | CERTAIN | Same warning, `store_true` semantics |
| `ExtendAction` | `__call__` | CERTAIN | `list.extend`/`PyList_Append` — repeatable flag accumulates values into one list |
| `SetArgumentsAction` | `__init__`, `__call__` | HIGH | Presence sets *multiple* downstream args at once (alias/preset expansion) |
| `RecordUsedAction` | `__call__` | HIGH | Marks a flag explicitly-set-by-user (the `_Used` sentinel; distinguishes user value from default) |
| `HelpAction` | `__call__` | CERTAIN | Drives normal `--help` (`format_help`) |
| `HelpHiddenAction` | `__call__` | CERTAIN | Drives `--help-hidden` → `print_help_hidden` |
| `HelpHiddenListAction` | `__call__` | CERTAIN | Drives `--help-hidden-list` → `print_help_hidden_list` |
| `str2bool` (helper) | module-level fn | CERTAIN | Coerces a string to bool; raises `"Boolean value expected."` on bad input |

### `NoableArgumentAction` — the `--no-X` generator

```c
function NoableArgumentAction___init__(self, option_strings, dest, default, ...):
    // for each positive "--X", synthesize the negation "--no-X"
    new_option_strings = []                       // literal 'new_option_strings'
    for opt in option_strings:                     // literal 'option_strings'
        new_option_strings.append(opt)             // keep "--X"
        new_option_strings.append("--no-" + strip_dashes(opt))  // add "--no-X" (literal "--no-")
    super().__init__(new_option_strings, dest, default=default, nargs=0, ...)

function NoableArgumentAction___call__(self, parser, ns, values, option_string):
    // sets dest True for "--X", False for "--no-X" — the positive default is `default`
    setattr(ns, self.dest, not option_string.startswith("--no-"))
```

The positive default of a Noable flag is the action's `default=` at registration, captured into the registry (`kind`/`job`/`default`). The `--no-*` companion literals seen in the [catalog](flag-catalog.md) (`--no-enable-tritium-loopfusion`, `--no-internal-hlo-remat`, `--no-keep-remat-dma-transpose`, `--no-run-pg-layout-and-tiling`) are exactly these synthesized negations.

### `EnableDisableArgumentAction` — the `--enable-X`/`--disable-X` pair

Mirror of `NoableArgumentAction` but using the `--enable`/`--disable` literals instead of `--no-`. It registers a positive `--enable-X` and a negative `--disable-X`; the dest resolves True/False on which spelling was given. Default again comes from `default=` at registration.

### `str2bool` — string→bool coercion

```c
function str2bool(v):                              // module-level, Actions.so
    if isinstance(v, bool): return v
    s = v.lower()                                  // interned __pyx_n_s_lower
    if s in {"true",  "t", "yes", "y", "1", "on"}:  return True   // interned "true"
    if s in {"false", "f", "no",  "n", "0", "off"}: return False  // interned "false", "no"
    raise ArgumentTypeError("Boolean value expected.")             // literal
```

The accepted token set (`true/t/yes/y/1/on` ↔ `false/f/no/n/0/off`) is recovered from the interned printable tokens (`true`, `false`, `no`, `t`, `f`, `y`, `n`, `1`, `0`, `on`) plus `__pyx_n_u_true` / `__pyx_n_u_false` and the `.lower()` normalization. `str2bool` is used as the `type=` for `--flag=true`/`--flag=false`-style options; `CompileCommand` also carries a local `str2bool` mirroring it.

> **NOTE —** `str2bool` raising `ArgumentTypeError("Boolean value expected.")` is what produces argparse's `argument --flag: Boolean value expected.` error rather than a silent `bool("false") == True` trap. A reimplementer that uses Python's `bool()` on the string directly inherits exactly that classic bug — `bool("false")` is `True`. The custom coercion exists to avoid it.

### Deprecation actions

```c
function DeprecatedArgumentAction___call__(self, parser, ns, values, option_string):
    warn(option_string + " is deprecated and will be removed in a future release.")  // literal
    // … then apply the value (or redirect to a replacement flag)
```

Both `DeprecatedArgumentAction` and `DeprecatedStoreTrueArgumentAction` emit the same `"is deprecated and will be removed in a future release."` literal (the `store_true` variant takes no value). The catalog's `--num-neuroncores` (deprecated → `--lnc`) is registered through this path.

### `ExtendAction` / `RecordUsedAction` / `SetArgumentsAction`

`ExtendAction.__call__` accumulates repeated-flag values into a single list via `extend`/`PyList_Append` — this is how repeatable flags like `--skip-pass=PassName` collect multiple names. `RecordUsedAction.__call__` sets the `_Used` sentinel (e.g. `num_parallel_jobs_Used`) so `buildPipeline` can tell a user-supplied value from a computed default — necessary because defaults are pre-seeded into the Namespace. `SetArgumentsAction` expands one flag into several downstream args (an alias/preset action); the class and its two methods are present in the symbol table, but the expansion behaviour is read from the name rather than the body [INFERRED].

---

## Evidence summary

| Claim | Anchor |
|---|---|
| `ArgKind` string pool holds `{PUBLIC, HIDDEN, INTERNAL, EARG, Harg}` | a word-boundary search of `Arguments.so` returns all five plus `ArgKind`, `argkind`, and `internal-`; the three with `__pyx_n_s_*` name symbols are the live-identifier subset, `EARG` appears as the interned value `__pyx_n_u_EARG`, `Harg` as a bare single-occurrence string |
| The `internal-` prefix rule | the interned literal `internal-` occurs once in `.rodata`, `startswith` is interned as `__pyx_n_s_startswith`, and both sit in `Arguments.so` next to the `_AddArgumentCallInterceptor.add_argument` qualname, with the `SetAttr` on the match branch |
| Three help surfaces, INTERNAL filter on `-list` | `print_help_hidden` @`0x1cc90` and `print_help_hidden_list` @`0x17150` are live qualnames; `ArgKind`, `SUPPRESS`, and the table literal `%-15s %-8s --%s: %s` are interned in the same module; `-list` carries a `kind == INTERNAL` comparison |
| The ten Action classes exist as named subclasses | all ten qualnames (`NoableArgumentAction`, `EnableDisableArgumentAction`, `DeprecatedArgumentAction`, `DeprecatedStoreTrueArgumentAction`, `ExtendAction`, `SetArgumentsAction`, `RecordUsedAction`, `HelpAction`, `HelpHiddenAction`, `HelpHiddenListAction`) plus `str2bool` are in the `Actions.so` symbol table |
| `str2bool` token set and error literal | interned tokens `true/false/no/t/f/y/n/on/1/0`, `__pyx_n_u_true` / `__pyx_n_u_false`, `__pyx_n_s_lower`, and the literal `Boolean value expected.` all present in `Actions.so` |

## Limits of this reading

- `Harg` is present in the pool but has no interned-symbol wrapper and no occurrence anywhere else in the driver tree. Treat it as a help-suppressed sub-kind, not as a member proven to be reached at runtime.
- The exact accept-set membership of `str2bool` — which of `on`/`off`/`yes`/`y` maps to which bool — follows from the token list; the per-token branch was not individually disassembled.
- No per-flag `HIDDEN`/`EARG` byte is asserted anywhere on this page. Those are set by explicit `kind=` kwargs inside `CompileCommand.__init__`, are not derivable from the flag name, and sit in a deeply nested body; the [flag catalog](flag-catalog.md) carries the per-flag visibility tags with their own confidence column.

---

## Related Components

| Name | Relationship |
|---|---|
| `_ArgumentRegistry` | Stores the `(argument, kind, job, default)` record the help paths walk |
| `_AddArgumentCallInterceptor` | Applies the `internal-` rule and forwards to argparse |
| `InterceptingArgumentParser` | Owns `print_help_hidden` / `print_help_hidden_list` |
| `argparse.SUPPRESS` | The mechanism that hides non-PUBLIC flags from ordinary `--help` |

## Cross-References

- [Flag Catalog](flag-catalog.md) — 3.8, the per-flag N–Z catalog whose `Vis {PUB/HID/INT/EARG}` column this taxonomy defines
- [The Two-Parser Architecture](two-parser-architecture.md) — 3.2, the driver vs Penguin parser split; the `_ArgumentRegistry` record and the three-member `ArgKind` subset this page widens to the full five-token pool
- [Sub-Tool argv Construction & Replay](subtool-argv.md) — 3.5, where the `job` column of each registry record is consumed
