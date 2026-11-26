// SPDX-License-Identifier: GPL-3.0-or-later
// NxModApi version 6: the appended get_interface member, its registry, and the layout every existing mod relies on.
// Ported from EmblemWeave cd1117d (XiONDiGiTaL), without its fixture mod DLL; this runtime's own name, obeliskchamber.list,
// is checked at the end.
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>

#include "nxrt/mod_api.h"
#include "nxrt/mod_interfaces.h"
#include "nxrt/mod_panels.h"
#include "nxrt/services_api.h"
#include "test_framework.h"

#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <filesystem>
#endif

NXTEST_GROUP(mod_api)

namespace {

// The version-5 table: every member before v6, in the same order (here the member after free_mem is open_list / add_panel,
// one member under two names: mod_api.h "Two version 4s"). These assertions are the
// compatibility test an existing mod binary relies on: version 6 appends one pointer and moves nothing.
struct NxModApiV5 {
    uint32_t version;
    uint32_t size;
    const char* game_id;
    uint64_t main_base;
    void (*log)(const NxModApi*, int, const char*);
    uint32_t (*hook_inline)(uint64_t, NxModInlineFn, void*);
    uint32_t (*hook_mid)(uint64_t, NxModSiteFn, void*);
    uint32_t (*hook_entry)(uint64_t, NxModSiteFn, void*);
    int (*hook_remove)(uint32_t);
    uint64_t (*call_guest)(Ctx*, uint64_t, const uint64_t*, int);
    int (*is_mapped)(uint64_t, uint64_t);
    void (*on_frame)(NxModFrameFn, void*);
    int (*is_function)(uint64_t);
    uint64_t (*make_function)(NxModFunctionFn, void*);
    int (*make_writable)(uint64_t, uint64_t);
    int (*edit_file)(const char*, NxModFileFn, void*);
    uint32_t (*mod_count)();
    const char* (*mod_folder)(uint32_t);
    int64_t (*read_file)(const char*, void*, uint64_t);
    int (*write_file)(const char*, const void*, uint64_t);
    int64_t (*zstd_decompress)(const void*, uint64_t, void*, uint64_t);
    int64_t (*zstd_compress)(const void*, uint64_t, void*, uint64_t, int);
    void* (*alloc)(uint64_t);
    void (*free_mem)(void*);
    int (*add_panel)(const char*, NxModPanelFn, void*);
    int (*panel_visible)();
};

#define SAME_OFFSET(field) static_assert(offsetof(NxModApi, field) == offsetof(NxModApiV5, field), #field " must not move")
SAME_OFFSET(version);
SAME_OFFSET(size);
SAME_OFFSET(game_id);
SAME_OFFSET(main_base);
SAME_OFFSET(log);
SAME_OFFSET(hook_inline);
SAME_OFFSET(hook_mid);
SAME_OFFSET(hook_entry);
SAME_OFFSET(hook_remove);
SAME_OFFSET(call_guest);
SAME_OFFSET(is_mapped);
SAME_OFFSET(on_frame);
SAME_OFFSET(is_function);
SAME_OFFSET(make_function);
SAME_OFFSET(make_writable);
SAME_OFFSET(edit_file);
SAME_OFFSET(mod_count);
SAME_OFFSET(mod_folder);
SAME_OFFSET(read_file);
SAME_OFFSET(write_file);
SAME_OFFSET(zstd_decompress);
SAME_OFFSET(zstd_compress);
SAME_OFFSET(alloc);
SAME_OFFSET(free_mem);
SAME_OFFSET(add_panel);
SAME_OFFSET(panel_visible);
#undef SAME_OFFSET

static_assert(sizeof(NxModApiV5) == offsetof(NxModApi, get_interface),
              "get_interface must be appended directly after panel_visible");
static_assert(sizeof(NxModApi) == offsetof(NxModApi, get_interface) + sizeof(NxModGetInterfaceFn),
              "get_interface is the last member of version 6");
static_assert(NXMOD_API_VERSION == 6, "this build implements version 6");

struct TestTable {
    uint32_t size;
    uint32_t version;
    uint64_t value;
};

TestTable g_table_v1{sizeof(TestTable), 1, 0x1111};
TestTable g_table_v2{sizeof(TestTable), 2, 0x2222};
TestTable g_table_v3{sizeof(TestTable), 3, 0x3333};

}  // namespace

NXTEST(version_and_layout) {
    CHECK_EQ(NXMOD_API_VERSION, 6u);
    CHECK_EQ(sizeof(NxModApi), offsetof(NxModApi, get_interface) + sizeof(NxModGetInterfaceFn));
    CHECK_EQ(offsetof(NxModApi, get_interface), sizeof(NxModApiV5));
    // The version-4 tail is still where existing mods read it.
    CHECK_EQ(offsetof(NxModApi, add_panel), offsetof(NxModApiV5, add_panel));
    CHECK_EQ(offsetof(NxModApi, panel_visible), offsetof(NxModApiV5, panel_visible));
    CHECK_EQ(offsetof(NxModApi, open_list), offsetof(NxModApi, add_panel));
}

NXTEST(registry_add_and_find) {
    nxrt::nxmod::InterfaceRegistry reg;
    CHECK(reg.empty());
    CHECK(reg.add("emblemweave.test", 2, &g_table_v2));
    CHECK_EQ(reg.count(), 1u);

    CHECK(reg.find("emblemweave.test", 2, nullptr) == &g_table_v2);
    CHECK(reg.find("emblemweave.test", 0, nullptr) == &g_table_v2);   // 0 = any
    CHECK(reg.find("emblemweave.test", 1, nullptr) == &g_table_v2);   // an older request gets the newer table
    CHECK(reg.find("emblemweave.test", 3, nullptr) == nullptr);       // that version does not exist
    CHECK(reg.find("emblemweave.other", 0, nullptr) == nullptr);
    CHECK(reg.find("EmblemWeave.test", 0, nullptr) == nullptr);       // case-sensitive
    CHECK(reg.find("", 0, nullptr) == nullptr);
    CHECK(reg.find(nullptr, 0, nullptr) == nullptr);
}

NXTEST(registry_version_compatibility) {
    nxrt::nxmod::InterfaceRegistry reg;
    CHECK(reg.add("obeliskchamber.list", 3, &g_table_v3));
    CHECK(reg.find("obeliskchamber.list", 1, nullptr) == &g_table_v3);
    CHECK(reg.find("obeliskchamber.list", 2, nullptr) == &g_table_v3);
    CHECK(reg.find("obeliskchamber.list", 3, nullptr) == &g_table_v3);
    CHECK(reg.find("obeliskchamber.list", 4, nullptr) == nullptr);
}

NXTEST(registry_rejects_bad_registrations) {
    nxrt::nxmod::InterfaceRegistry reg;
    CHECK(!reg.add(nullptr, 1, &g_table_v1));
    CHECK(!reg.add("", 1, &g_table_v1));
    CHECK(!reg.add("nxp", 1, &g_table_v1));           // one segment
    CHECK(!reg.add("nxp.", 1, &g_table_v1));          // trailing dot
    CHECK(!reg.add(".nxp.ui", 1, &g_table_v1));       // leading dot
    CHECK(!reg.add("nxp..ui", 1, &g_table_v1));       // empty segment
    CHECK(!reg.add("NXP.ui", 1, &g_table_v1));        // upper-case
    CHECK(!reg.add("nxp.-ui", 1, &g_table_v1));       // segment starts with '-'
    CHECK(!reg.add("nxp.ui_", 1, &g_table_v1));       // not a segment character
    CHECK(!reg.add("nxp/ui", 1, &g_table_v1));
    CHECK(!reg.add("nxp ui", 1, &g_table_v1));
    CHECK(!reg.add("nxp.ui", 0, &g_table_v1));        // interface versions start at 1
    CHECK(!reg.add("nxp.ui", 1, nullptr));            // a table is required
    CHECK(!reg.add_resolver("nxp.ui", 1, nullptr));   // a resolver is required
    CHECK(reg.empty());

    std::string at_limit(120, 'a');
    at_limit += ".b";                                 // 122 bytes: still valid
    CHECK(nxrt::nxmod::valid_name(at_limit.c_str()));
    std::string too_long(127, 'a');
    too_long += ".b";                                 // 129 bytes: over the limit
    CHECK(!nxrt::nxmod::valid_name(too_long.c_str()));
}

NXTEST(registry_refuses_duplicates) {
    nxrt::nxmod::InterfaceRegistry reg;
    CHECK(reg.add("emblemweave.dup", 1, &g_table_v1));
    CHECK(!reg.add("emblemweave.dup", 1, &g_table_v1));   // same table is still a second registration
    CHECK(!reg.add("emblemweave.dup", 2, &g_table_v2));   // never replace the first table
    CHECK_EQ(reg.count(), 1u);
    CHECK(reg.find("emblemweave.dup", 0, nullptr) == &g_table_v1);
}

NXTEST(registry_resolver) {
    nxrt::nxmod::InterfaceRegistry reg;
    CHECK(reg.add_resolver("nxp.nxmod", 1, [](const void* context, void*) { return context; }));
    int sentinel = 0;
    CHECK(reg.find("nxp.nxmod", 0, &sentinel) == &sentinel);
    CHECK(reg.find("nxp.nxmod", 1, &sentinel) == &sentinel);
    CHECK(reg.find("nxp.nxmod", 2, &sentinel) == nullptr);
    CHECK(reg.find("nxp.nxmod", 1, nullptr) == nullptr);
}

NXTEST(bridge_nxp_nxmod) {
    NxModApi api{};
    api.version = NXMOD_API_VERSION;
    api.size = sizeof(NxModApi);
    CHECK(api.version >= 6);

    // The bridge answers the caller's own table, not a copy.
    CHECK(nxrt::nxmod::get_interface(&api, nxrt::nxmod::kNativeApi, 0) == &api);
    CHECK(nxrt::nxmod::get_interface(&api, nxrt::nxmod::kNativeApi, 1) == &api);
    CHECK(nxrt::nxmod::get_interface(&api, nxrt::nxmod::kNativeApi, 2) == nullptr);
    CHECK(nxrt::nxmod::get_interface(nullptr, nxrt::nxmod::kNativeApi, 0) == nullptr);

    // Another host's name is not synthesized by this host.
    CHECK(nxrt::nxmod::get_interface(&api, "emblemweave.ui.panel", 0) == nullptr);
    CHECK(nxrt::nxmod::get_interface(&api, "fefw-recomp.sites", 1) == nullptr);
    CHECK(nxrt::nxmod::get_interface(&api, "NXP.nxmod", 0) == nullptr);
    CHECK(nxrt::nxmod::get_interface(&api, "", 0) == nullptr);
    CHECK(nxrt::nxmod::get_interface(&api, nullptr, 0) == nullptr);

    // A table registered by the host is reachable through the same lookup.
    CHECK(nxrt::nxmod::interfaces().add("obeliskchamber.probe", 2, &g_table_v2));
    CHECK(nxrt::nxmod::get_interface(&api, "obeliskchamber.probe", 0) == &g_table_v2);
    CHECK(nxrt::nxmod::get_interface(&api, "obeliskchamber.probe", 3) == nullptr);
}

// This runtime's own name: obeliskchamber.list (version 1) is open_list by name, registered before mods load.
NXTEST(obeliskchamber_list) {
    nxrt::services::modapi::register_interfaces();
    nxrt::services::modapi::register_interfaces();   // a second call registers nothing (and is no failure)
    NxModApi api{};
    api.version = NXMOD_API_VERSION;
    api.size = sizeof(NxModApi);
    const auto* list = static_cast<const NxModListApi*>(nxrt::nxmod::get_interface(&api, NXMOD_LIST_INTERFACE, 1));
    REQUIRE(list != nullptr);
    CHECK_EQ(list->size, static_cast<uint32_t>(sizeof(NxModListApi)));
    CHECK_EQ(list->version, 1u);
    CHECK(list->open_list != nullptr);
    CHECK(nxrt::nxmod::get_interface(&api, NXMOD_LIST_INTERFACE, 0) == list);
    CHECK(nxrt::nxmod::get_interface(&api, NXMOD_LIST_INTERFACE, 2) == nullptr);
    CHECK(nxrt::nxmod::get_interface(&api, "oc.list", 0) == nullptr);   // aliases are for input and messages, not for lookup
    // no game module has set a list opener in the test executable: the screen cannot be made, and the call says so
    CHECK_EQ(list->open_list(nullptr, 0, nullptr), 0);
}
