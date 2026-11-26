// SPDX-License-Identifier: GPL-3.0-or-later
// The code mods' panels (mod API version 5, nxrt/mod_api.h add_panel; EmblemWeave's version 4): services/mods_dll.cpp keeps them, the
// overlay draws them in its Mods window ([gfx] mods_key) and the menu on its Mods tab (gfx/overlay.cpp draw_mod_panels).
#pragma once
#include <string>
#include <vector>

#include "nxrt/mod_api.h"

namespace nxrt::services::modapi {

struct PanelInfo {
    std::string title;           // as the mod gave it
    std::string mod;             // the mod that added it (folder/dll), for the tooltip
    NxModPanelFn fn = nullptr;
    void* user = nullptr;
};

// The panels of the mods that started, in the order added (a copy: any thread)
std::vector<PanelInfo> panels();
// Whether there is any (cheap: the menu asks every frame)
bool has_panels();

// The NxModApi member after free_mem (open_list here, add_panel in EmblemWeave; mod_api.h "Two version 4s"): open_list when a is the
// calling game thread's Ctx, add_panel otherwise. What every mod's api->open_list / api->add_panel points to.
int list_or_panel(void* a, void* b, void* c);

// This runtime's named interfaces (version 6): obeliskchamber.list. load_dll_mods calls it before any mod loads; any later call does nothing.
void register_interfaces();

// The overlay says whether a panel is on screen (api->panel_visible)
void set_panel_visible(bool visible);

}  // namespace nxrt::services::modapi
