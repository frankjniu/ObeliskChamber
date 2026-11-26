# Code-mod interfaces (NxModApi version 6)

Ported from EmblemWeave's `docs/MOD_INTERFACES.md` (cd1117d, XiONDiGiTaL): the three ports agreed on version 6 in
fefw-recomp issue #165, and the rules below are shared. The parts that are ObeliskChamber's own are marked.

ObeliskChamber's code-mod table (`include/nxrt/mod_api.h`) is at version 6. Version 6 appends one member,
`get_interface`, and changes nothing else: every earlier member keeps its position, type and meaning, so a mod
compiled against version 3, 4 or 5 keeps working without recompilation.

```c
typedef const void* (*NxModGetInterfaceFn)(const struct NxModApi* api, const char* name, uint32_t version);
```

`get_interface` is at the end of `NxModApi`, after `panel_visible`. A mod checks `api->version >= 6` and
`api->size >= offsetof(NxModApi, get_interface) + sizeof(api->get_interface)` before reading it, as for every
versioned member.

## Lookup rules

- **Name**: ASCII, case-sensitive, canonical `<owner>.<local-name>` with at least two dot-separated segments.
  Segments use `[a-z0-9-]` and start with a letter or digit. Upper-case, empty segments, a leading or trailing dot,
  and names over 127 bytes are invalid.
- **Version**: the caller passes the interface version it was written for. `0` asks for any compatible version.
  An interface table is append-only, so a table at version N also answers a request for an older version. A request
  for a version newer than the table the host provides returns `NULL`.
- **Existence probe**: `get_interface(api, name, 0) != NULL`. There is no separate `has()` member.
- **Invalid, unknown and incompatible names all return `NULL`**; a mod must not treat a miss as fatal.
- **Tables start with `uint32_t size` and `uint32_t version`** and grow only at the end. Check both before reading a
  member added after the version the caller was written for.
- **Thread and phase**: the lookup may be called from `nxmod_early`, `nxmod_init`, a hook or a frame callback, on any
  thread. A returned table stays valid for the run.

## Names and ownership

A name belongs to the project that defines it. A host answers for its own prefix and for the shared names it
implements; it does not synthesize another host's names.

| Name | Owner | Meaning |
| --- | --- | --- |
| `nxp.nxmod` | NXP (shared) | Version 1. The host's own `NxModApi` table. |
| `nxp.ui` | NXP (shared) | The portable panel canvas (Trinity's adapter). |
| `nxp.settings`, `nxp.files`, `nxp.frame` | NXP (shared) | The other NXP v1 services (Trinity's adapter). |
| `emblemweave.*` | EmblemWeave | Host-only interfaces and game integrations. |
| `fefw-recomp.*` | fefw-recomp | Host-only interfaces and game integrations. |
| `obeliskchamber.*` | ObeliskChamber | Host-only interfaces, e.g. `obeliskchamber.list` version 1. |

This runtime answers `nxp.nxmod` (version 1) and `obeliskchamber.list` (version 1). It does not answer `nxp.ui`,
`nxp.settings`, `nxp.files` or `nxp.frame` itself yet: Trinity's adapter builds those from this table.

### `obeliskchamber.list` (version 1, ObeliskChamber)

```c
#define NXMOD_LIST_INTERFACE "obeliskchamber.list"
typedef struct NxModListApi {
    uint32_t size;      /* sizeof(NxModListApi) of the runtime */
    uint32_t version;   /* 1 */
    int (*open_list)(Ctx* c, uint64_t item, const struct NxModList* list);
} NxModListApi;
```

`open_list` is the list screen of version 4: called from the confirm handler of one of the game's menu items with that
item and the handler's `Ctx`, it opens a screen of rows in the game's own style and returns 1, or 0 when the game module
offers none or it cannot be made. In versions 4 and 5 it shares its member with `add_panel` (mod_api.h, "Two version 4s");
that member stays for mods built before version 6, but a new mod asks for the list screen by name:

```c
const NxModListApi* list = NULL;
if (api->version >= 6 && api->size >= offsetof(NxModApi, get_interface) + sizeof(api->get_interface))
    list = (const NxModListApi*)api->get_interface(api, NXMOD_LIST_INTERFACE, 1);
if (list) list->open_list(c, item, &my_list);   /* NULL on EmblemWeave and fefw-recomp: no list screen there */
```

`nxp.nxmod` is the bridge to the legacy table and is registered by the runtime itself. It answers the `api` pointer
the caller was given: the table itself, not a copy. That table keeps its existing `version`-then-`size` order and
its own version field; it is the one bridged table, not a new interface structure. A plugin built on a shared layer
gets hooks, `call_guest`, guest memory and file edits through it on every host that has version 6.

`get_interface(api, "nxp.nxmod", 1) == api` is the expected result. Version 2 of that name does not exist.

## Registration

Registration lives in `include/nxrt/mod_interfaces.h` and `src/services/mod_interfaces.cpp`.

```cpp
nxrt::nxmod::InterfaceRegistry& r = nxrt::nxmod::interfaces();
r.add("emblemweave.ui.panel", 1, &panel_table);          // a fixed table
r.add_resolver("nxp.nxmod", 1, resolve_native_api);     // a table that depends on the caller
```

Rules enforced by the registry:

- the name must be canonical (see above) and the version must be at least 1;
- the table (or resolver) must not be null;
- a name is registered once. A second registration is refused, including the same table at another version:
  replacing a live table would give two mods different answers for one name. A duplicate is a host bug, not a
  negotiation;
- a resolver receives the `NxModApi` passed to `get_interface`; the registry lock is not held while it runs, and its
  result must stay valid for the run.

A host adds its interfaces before it loads mods. `nxp.nxmod` version 1 is registered on first use; here
`obeliskchamber.list` is added by `register_interfaces()` (services/mods_dll.cpp) at the start of `load_dll_mods`.

## Version 5 here and EmblemWeave's version 4

ObeliskChamber's version 5 is EmblemWeave's version 4 (`add_panel`, `panel_visible` at the same places), except that the
`add_panel` member is also this runtime's version-4 `open_list`: the runtime tells the two apart by the first argument
(the calling game thread's `Ctx` for `open_list`, a title for `add_panel`). EmblemWeave goes from 4 to 6 and never had
that meaning. A mod must not infer `open_list` from `api->version >= 5`; from version 6 it asks for
`obeliskchamber.list` by name, which every other host answers with NULL.

## Tests

`tests/test_mod_api.cpp` (group `mod_api`, ported from EmblemWeave without its fixture mod DLL) covers:

- compile-time offsets for every version-3/4/5 member, `get_interface` as the last member, and `open_list` sharing
  `add_panel`'s member;
- registration, lookup, version 0, older/newer requests, unknown and invalid names, duplicates and bad versions;
- the `nxp.nxmod` bridge through the real `get_interface`, including names this host does not own;
- `obeliskchamber.list`: registered once, its table's size and version, and that an alias (`oc.list`) is not a lookup
  name.
