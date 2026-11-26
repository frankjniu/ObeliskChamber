// SPDX-License-Identifier: GPL-3.0-or-later
// The code mods' panels (mod API version 5; EmblemWeave's version 4, its #71): the interface's layout matches EmblemWeave's member for
// member, and the member after free_mem (open_list here, add_panel there) adds a panel when it is not given the calling thread's Ctx.
#include <cstddef>
#include <cstring>
#include <string>

#include "nxrt/mod_api.h"
#include "nxrt/mod_panels.h"
#include "test_framework.h"

NXTEST_GROUP(mod_panels)

namespace modapi = nxrt::services::modapi;

// EmblemWeave's layout: add_panel right after free_mem, panel_visible right after it; NxModUi a size and 14 functions.
static_assert(offsetof(NxModApi, add_panel) == offsetof(NxModApi, free_mem) + sizeof(void*), "add_panel follows free_mem");
static_assert(offsetof(NxModApi, open_list) == offsetof(NxModApi, add_panel), "open_list and add_panel share their member");
static_assert(offsetof(NxModApi, panel_visible) == offsetof(NxModApi, add_panel) + sizeof(void*), "panel_visible follows add_panel");
static_assert(offsetof(NxModApi, get_interface) == offsetof(NxModApi, panel_visible) + sizeof(void*), "get_interface follows panel_visible (version 6)");
static_assert(offsetof(NxModUi, text) == 8 && offsetof(NxModUi, tooltip) == 8 + 13 * sizeof(void*) && sizeof(NxModUi) == 8 + 14 * sizeof(void*),
              "NxModUi as in EmblemWeave");
static_assert(NXMOD_API_VERSION >= 5, "panels are version 5 here");

namespace {
int g_drawn = 0;
void panel(const NxModUi*, void* user) { g_drawn += *static_cast<int*>(user); }
}  // namespace

NXTEST(add_panel_from_a_host_thread) {
    static int one = 1;
    static const char kTitle[] = "Test panel (mod_panels)";
    // a host thread has no Ctx: the shared member is add_panel
    CHECK_EQ(modapi::list_or_panel(const_cast<char*>(kTitle), reinterpret_cast<void*>(&panel), &one), 1);
    CHECK(modapi::has_panels());
    bool found = false;
    for (const modapi::PanelInfo& p : modapi::panels()) {
        if (p.title != kTitle) continue;
        found = true;
        CHECK(p.fn == &panel);
        CHECK(p.user == &one);
        g_drawn = 0;
        p.fn(nullptr, p.user);
        CHECK_EQ(g_drawn, 1);
    }
    CHECK(found);
    // missing arguments: nothing added
    CHECK_EQ(modapi::list_or_panel(nullptr, reinterpret_cast<void*>(&panel), &one), 0);
    CHECK_EQ(modapi::list_or_panel(const_cast<char*>(""), reinterpret_cast<void*>(&panel), &one), 0);
    CHECK_EQ(modapi::list_or_panel(const_cast<char*>(kTitle), nullptr, &one), 0);
}
