# Obelisk Chamber

<a href="https://recomplauncher.com"><picture><source media="(prefers-color-scheme: dark)" srcset="https://recomplauncher.com/assets/press/launcher/badge-light.svg"><img alt="Get it on RecompLauncher" src="https://recomplauncher.com/assets/press/launcher/badge-dark.svg" height="48"></picture></a>

A native PC version of **Fire Emblem: Fortune's Weave** (Nintendo Switch 2), made by static recompilation. The game's code is
translated to C++ and compiled into a regular Windows program, which runs on a small runtime that stands in for the console's system
software and draws with Vulkan. It is not an emulator.

Obelisk Chamber is a continuation of **fefw-recomp by Goldoire**. It adds menus inside the game's own interface, save slots, a mod
menu, more picture and performance options and a redesigned launcher. The program, settings and package files carry the new name
(`ObeliskChamber.exe`, `ObeliskChamber.ini`, `ObeliskChamber-launcher.exe`); the runtime DLL is still `fefw_rt.dll` and the game's
options still sit under `[fefw]`, so existing mods and settings keep working. A settings file from before the rename (`fefw.ini`) is
copied to the new name the first time the game or the launcher starts.

> [!IMPORTANT]
> **No game files are included or downloadable here.** This project contains only its own tools and runtime. To play, you need your
> own legally dumped copy of the game; everything that reads the game happens on your PC. See [LEGAL.md](LEGAL.md).
>
> Not affiliated with or endorsed by Nintendo or Intelligent Systems.

## Where this comes from

Almost all of this project is the original **[fefw-recomp](https://github.com/Goldoire/fefw-recomp) by Goldoire** and its
contributors. Goldoire wrote, when publishing the last version (0.2.0), that the repository would be deleted and that anyone is free
to continue the work. This repository does that; it was forked from a copy of the original at release 0.1.7 and later merged 0.2.0.

How much comes from where, measured on 2026-11-17 (release 0.3.0) by the lines of the repository's own text files as `git blame`
attributes them over the full history (third-party folders and lock files left out, 416,765 lines).

| | Lines | Share |
|---|---|---|
| The original fefw-recomp: Goldoire | 386,033 | 92.6% |
| The original fefw-recomp: its contributors (Stellarznith, Aidan Kinzett, Paulius / Kompas9, ranbinrang-rgb, Abubakar-ux) | 6,110 | 1.5% |
| [Kompas9's fork](https://github.com/Kompas9/fefw-recomp) (Paulius), changes brought in here | 413 | 0.1% |
| [Stephen Jayakar's fork](https://github.com/stephenjayakar/fefw-recomp), changes brought in here (4,457 of them two lists of hot function addresses) | 9,765 | 2.3% |
| Obelisk Chamber (this fork): its maintainer | 13,713 | 3.3% |
| Obelisk Chamber (this fork): contributors (ranbinrang-rgb) | 731 | 0.2% |

So about **94.1% of the code is the original's, 0.1% comes from Kompas9's fork, 2.3% from Stephen Jayakar's fork and about 3.5%
was added here**. By commits, not counting merges, the split is 84 from the original, 17 from Kompas9's fork, 64 from Stephen
Jayakar's fork, 152 from this fork's maintainer and 8 from its contributors. Lines are a rough measure of
work, and the original in turn builds on the projects named under [Credits](#credits) (its first commit holds their code too, so
their share is inside the 94.1%). Techniques taken from other ports and mods (written here, not copied) count as this fork's lines;
they are credited under [Credits](#credits).

The maintainer's changes in this fork were written with Claude (Anthropic's AI model), directed and tested by the maintainer.

## Features

### From the original fefw-recomp

- Runs natively on Windows, with no emulator in between; Linux and Steam Deck through Proton
- 60 or 120 FPS modes, any output resolution, render scale and a temporal upscaler
- DLSS, FSR 3 and XeSS upscaling, and DLSS or FSR 3 frame generation, with files you supply
- Ultrawide (21:9, 32:9 or any wider shape): a wider view with the menus and HUD kept at 16:9 in the centre
- Windowed, borderless or fullscreen
- Keyboard, mouse (through the game's own mouse controls) or any controller, with matching button prompts
- An in-game settings overlay (**F1**)
- Every game language, DLC from your own dump, RomFS mods and code mods as DLLs
- Movies, volume up to 200%, and the game's photos saved to an album folder
- A launcher that sets everything up from your dump, with nothing else to install, and updates itself

### Added in Obelisk Chamber

Release by release: [docs/releases](docs/releases) (0.3.0 onwards; earlier notes are on the [Releases](../../releases) page).

**Inside the game's own menus**
- **Recomp Options**: a screen under **+ > System**, built from the game's own Settings widgets, with Display, Graphics, Frame Rate
  and Audio tabs. Most settings apply to the running game (render scale, shadow resolution, quality, frame rate, dynamic resolution
  and more); **-** restores the defaults after a confirmation
- **Quit Game**: the last entry under **+ > System**. It asks in the game's own yes / no dialog, then ends the game the same way as
  the F1 menu's **Quit game**
- **Save slots**: several saves side by side, chosen from the game's own Continue, New Game and Delete Save entries on the title
- **Save and Load lists** in the + menu: one press saves or loads a slot through the game's own prompts, and each row shows the
  chapter and play time
- **DLC / Mods** on the title screen: every mod listed on a RomFS tab and an ExeFS tab, switched on or off there

**Picture and performance**
- **Graphics presets** (Low / Medium / High / Ultra) in the launcher, the F1 menu and Recomp Options. Low also turns down the game's
  own reflections, ambient occlusion, crowd and scenery detail (`[fefw] reflections`, `ambient_occlusion`, `crowd_detail`,
  `scenery_detail`), which edit the game's graphics settings files as they load
- **Frame-rate fixes** (`[fefw] frame_fixes`): at 60 and 120 fps, capes, hair, physics, cutscene camera shake, animation eases, map
  markers, menu timers and dialogue typing keep their real speed instead of running 2x or 4x fast
- **The game's own HDR picture** (`[hdr] mode = native`): the game is told the display is HDR, as on a Switch 2 with an HDR TV, and its
  10-bit PQ frames go to the screen unchanged
- Settings that the running game can take apply at once from the F1 menu and Recomp Options (presets, render scale, shadows,
  quality, frame rate, dynamic resolution, upscaler)
- **Intel GPUs** (Arc and integrated): the paths that lost the GPU at the title screen are turned off by themselves
  (`[gfx] intel_compat`)
- Faster loading and fewer hitches: textures stream in behind quick stand-ins, shaders are kept in memory and built ahead, the
  render thread's Vulkan calls run on a worker, fewer GPU barriers, faster translated game code (most of it from Stephen Jayakar's
  fork, see [Credits](#credits)), and cheaper per-draw checks
- Defaults that follow the hardware: worker threads, spin waits and background shader threads scale with the CPU; DLSS model E on
  RTX 20 / 30 cards; DLSS that recovers after a failure under video memory pressure
- A DLSS model choice that applies live
- Dynamic resolution that lowers the render size only where it gains frame rate
- Coarser shading for chosen draws, and options to skip or spread repeated compute work
- A **Handheld** quality choice (the console's handheld reductions) beside Docked
- Speakers: stereo or 5.1
- Lower runtime overhead in three hot paths, and a corrected input-delay figure in the overlay
- Movies decoded with dav1d's own assembly and a deeper decode buffer, so the 1440p cutscenes stay in step with their sound
- Ultrawide: framed panels, the Stats / roster frame and the World Map picture keep their 16:9 shape while dims and full-screen
  backgrounds still cover the whole screen, and the world / quest map's location dots, routes and route outlines stay on the map.
  Full-screen pictures (title, loading screens, illustrations) keep their shape (`[fefw] ultrawide_images`), and the output lists
  are grouped by shape (16:9, 21:9, 32:9) with a "This monitor" entry
- An F1 menu that no longer dims the game (so picture settings can be judged while changing them) and can be moved with the mouse
  or the right stick

**Mods**
- Code patches (`.ips`, IPS32, `.pchtxt`) from a mod's `exefs` folder are applied when the game is built
- DLL mods can bring hook sites of their own (`exefs/*.sites`, added by Set up; contributed by ranbinrang-rgb), give the game
  functions to call, edit the game's data files as they are loaded, and open a list screen in the game's style (mod interface
  versions 2 to 4)

**Launcher**
- Redesigned after the game's menus: a title-style list, framed panels with tabs, a help box and key help; keyboard and controller
  navigation as in the game
- An option to draw the launcher with the game's own menu art, read from your dump when the launcher starts (nothing is copied)
- New name and icon

**For people who build it themselves**
- Profile-guided optimisation and ThinLTO for the game code, a profile-guided runtime, and per-PC build tuning
  (`build_options.json`); see [docs/PGO.md](docs/PGO.md)
- A release runtime (`fefw_rt.dll`) built with a profile and ThinLTO; the game code itself stays a general build
- Repeatable performance runs: scripted routes, an A/B runner and a report ([docs/PERF_ROUTES.md](docs/PERF_ROUTES.md))
- A CI build of the runtime on every change, and a runtime test suite that passes in full

> [!NOTE]
> This fork was made for personal use, and its releases are play-tests: they have had one person's testing on one PC, and updates
> come when the maintainer wants something for their own play. The redesigned launcher is new and has had little testing.
>
> A release is a general build that runs on any supported processor. If you know how to build from source, that is the better
> option: profile-guided optimisation, ThinLTO and tuning for your own CPU are available there. See
> [docs/BUILDING.md](docs/BUILDING.md) and [docs/PGO.md](docs/PGO.md).

## Requirements

- Windows 10 or 11, 64-bit
- A 64-bit CPU with SSE4.2 and POPCNT: an Intel Core i3 / i5 / i7 (2008) or newer, or an AMD FX (2011) / Ryzen or newer. Setup builds the game for your processor: CPUs with FMA3 (Intel Haswell 2013, AMD Piledriver 2012 and newer) get the faster build, older ones such as Sandy Bridge, Ivy Bridge and Xeon E5 v1 / v2 a compatible build that plays the same, a little slower. Older processors (Core 2, AMD Phenom) are not supported; Setup and `ObeliskChamber.exe` say so instead of crashing
- A GPU with Vulkan 1.3 and an up-to-date driver
- 16 GB of RAM and about 30 GB of free disk space
- Your own decrypted dump of the game at update **131072**, in one of these forms:
  - a folder with `exefs\` and `romfs\`
  - a dump folder with `base\` and `updates\<version>\`
  - a [`.nup`](https://github.com/a55uka/nup) package

## Getting started

1. Download `ObeliskChamber-<version>-win64.zip` from the [Releases](../../releases) page and extract it to its own folder.
2. Run `ObeliskChamber-launcher.exe`.
3. On **Setup**, choose your dump folder (or **.nup** file) and press **Set up**. The launcher checks your files, builds the game on
   your PC and prepares the shaders. The first setup takes a while; you can **Pause** it and continue later.
4. Press **Play**.

Your saves are in the `save\` folder next to the launcher.

### Linux and Steam Deck

Download `ObeliskChamber-<version>-linux-x64.tar.gz` instead, extract it and run `ObeliskChamber-launcher.AppImage`; the steps are the same. The
game runs through Proton: Play needs Steam with a Proton installed, [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) (`umu-run`) or Wine, or
press **Add to Steam** on the Play page and start it from Steam (also in the Steam Deck's Game Mode). After an update, run Set up in
the launcher (Desktop Mode on a Deck) before playing from Steam.

### Settings

The launcher's **Settings** page covers resolution, frame rate, upscaling, window mode, sound and controls. Press **F1** in game for
the overlay, or open **+ > System > Recomp Options**. [docs/SETTINGS_GUIDE.md](docs/SETTINGS_GUIDE.md) lists the settings worth
changing, what each does and who gains from changing it (slower GPUs, small CPUs, Intel, ultrawide, HDR). Every option is described in
[runtime/docs/CONFIG.md](runtime/docs/CONFIG.md).

### Game language

**Settings > Language** (or **F1 > Game**) picks the language of the game's text and voices: Japanese, English (US or UK), French,
German, Italian, Spanish, Korean and Chinese (Simplified or Traditional). *Automatic*, the default, follows your Windows display
language and uses English when the game has no translation for it. The change applies the next time the game starts.

### DLSS (NVIDIA RTX only)

NVIDIA's files cannot be shipped here, so DLSS stays off until you add them. Take them from NVIDIA's
[Streamline SDK](https://github.com/NVIDIAGameWorks/Streamline) (v2.x, the `bin/x64` folder of the release, not the development one) and
put these four files in a folder named `upscalers\streamline` next to `ObeliskChamber-launcher.exe`:

```
sl.interposer.dll   sl.common.dll   sl.dlss.dll   nvngx_dlss.dll
```

Then choose **DLSS** (or **Automatic**, which picks DLSS when the files and an RTX GPU are there) as the Upscaler in **Settings**, and
restart the game. `ObeliskChamber.log` says `Streamline ... started` and `DLSS (Streamline)` when it works. The folder can be somewhere else:
set it under **Settings > Picture > DLSS (Streamline) folder**.

For DLSS frame generation (RTX 40 or newer, with Windows' hardware-accelerated GPU scheduling on) add five more files from the same
folder: `sl.dlss_g.dll`, `sl.reflex.dll`, `sl.pcl.dll`, `nvngx_dlssg.dll` and `NvLowLatencyVk.dll`.

On an RTX 30 or older GPU, NVIDIA's own files refuse frame generation. Some community mods replace some of these files to run it on
older GPUs. We don't ship or support them. If you use one, put its files in the same `upscalers\streamline` folder, replacing the
originals as the mod says, and set `frame_gen_any_gpu = 1` under `[dlss]` in `ObeliskChamber.ini` in the run folder (or the environment
variable `NXRT_DLSS_FRAME_GEN_ANY_GPU=1`). The game then no longer turns frame generation off just because Streamline says the GPU
is too old, and the mod's own answer decides. `ObeliskChamber.log` shows the outcome on its `framegen` and `dlss` lines.

### FSR 3 (any GPU)

FSR 3 needs AMD's signed FidelityFX files, which you download from AMD's
[FidelityFX SDK](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK) repository. Put them in `upscalers\fsr` next to
`ObeliskChamber-launcher.exe` (create the folder):

```
amd_fidelityfx_upscaler_vk.dll   amd_fidelityfx_loader_vk.dll   FSR 3 upscaling (Kits/FidelityFX/signedbin of tag v2.4.0)
amd_fidelityfx_vk.dll                                            FSR 3 frame generation (PrebuiltSignedDLL of tag v1.1.4)
```

Choose **AMD FSR 3 upscaling** as the Upscaler and/or **AMD FSR 3 frame generation** under **Settings > Frame rate**, then restart
the game. Frame generation adds one generated frame between two real ones; the HUD and text are taken from the real frames, so they
stay sharp. It works best with VSync off on a 120 Hz or faster display: with VSync on the game renders at most half the refresh
rate, and on a display slower than twice the game's frame rate generation pauses. **Settings > Picture > FSR 3 (AMD) folder** shows
whether the files are found, and can point to another folder. If a file is missing, `ObeliskChamber.log` names it and the game carries on with TAAU (or without frame generation).

### XeSS (any GPU, best on Intel Arc)

XeSS needs Intel's `libxess.dll`, which you download from `bin/` of the [XeSS SDK 3.0.2](https://github.com/intel/xess/releases/tag/v3.0.2)
release. Put it in `upscalers\xess` next to `ObeliskChamber-launcher.exe` (create the folder). It also needs the
[Microsoft Visual C++ 2015-2022 Redistributable (x64)](https://aka.ms/vs/17/release/vc_redist.x64.exe). Then choose **Intel XeSS** as the
Upscaler in **Settings** and restart the game. If the file or the runtime is missing, `ObeliskChamber.log` says so and the game uses TAAU.

### Keyboard and controller

Any controller works, and so does the keyboard, with no setup. The button prompts in the game follow the one you used last: press a
key and they show keys, touch the controller and they show its buttons. The keyboard drives the Switch buttons the game knows:

| Keys | Switch | |
|---|---|---|
| W A S D | Left stick | Move |
| Arrow keys, I J K L | Right stick | Camera |
| Enter, Space | A | Confirm |
| Esc, Backspace | B | Back |
| R | X | |
| F | Y | |
| Q / E | L / R | E: dash in town |
| Z / C | ZL / ZR | C: map in town |
| Tab | + | Menu |
| M | - | |
| 1 2 3 4, or numpad 8 2 4 6 | D-pad up, down, left, right | |
| V / B | Left / right stick click | |

The movement keys push the stick all the way. **F1** (settings), **F2** (frame rate counter) and **Alt+Enter** (full screen) are
the runtime's own keys. **F1 > Controls** lists the keys as they are now. To leave the game cleanly, use **Quit game** at the
bottom of the F1 menu (click it twice), **Quit Game** under **+ > System**, or close the window (**Alt+F4**): each saves the settings
and ends the game normally.

To change a key, close the game and edit `[bindings.all]` in `controls.ini` in the run folder (the game creates it on its first
start). Each line is `key = action`:

```
[bindings.all]
W = move_forward
Return = btn_a
G = btn_x
X = none
```

Left side: a key name (`Return`, `Space`, `Left Shift`, `Up`, `Keypad 8` ...). Right side: `move_*`, `look_*`, `dpad_*`, `btn_a`,
`btn_b`, `btn_x`, `btn_y`, `btn_l`, `btn_r`, `btn_zl`, `btn_zr`, `btn_plus`, `btn_minus`, `btn_lstick`, `btn_rstick`, or `none` to
switch a key off. An older `controls.ini` gets the keys above added at the next start (only keys you have not bound yourself; the
old file is kept as `controls.ini.bak`). To go back to the default keys, delete the `[bindings.*]` sections. Controller options (stick dead zones,
button mapping, prompts) are under **Settings** and **F1 > Controls**.

### Mouse

The game has its own mouse controls, made for the Switch 2's Joy-Con 2 used as a mouse, and the PC mouse drives them. Move the mouse
or click and the game switches to its mouse controls (its prompts show mouse buttons); press a key or touch the controller and it
switches straight back. Whichever you used last is in control.

- **Bases, dungeons:** the mouse turns the camera, left click talks, opens and attacks, right click cancels or opens the menu, the
  wheel zooms the camera while standing still. Walk with WASD or the stick.
- **Battle maps:** the mouse moves the map cursor (faster when you move it faster), left click selects and confirms, right click
  cancels.
- **Menus:** moving the mouse up or down moves the selection, left click confirms, right click goes back.

While the game is in its mouse controls the Windows pointer is hidden and kept in the window (it comes back where the game's pointer
was). A key, the controller, **Alt+Tab** or **F1** frees it. The game's own **System > Mouse Controls** page shows every button, and
its **System > Settings** set the speeds: the rows marked (Mouse) on the General, Battle and Bases/Dungeons tabs (camera
sensitivity, map cursor speed, menu cursor speed), and **Method for Switching to Mouse**, which should stay automatic. Leave the
game's angle calibration alone (a desk mouse needs none; **Reset Calibration** undoes it). **F1 > Controls > Mouse** has the PC side:
mouse controls automatic, always or off, and the mouse speed before the game's own (`[mouse] jc2_speed` in `controls.ini`).

On battle maps, hold **Y** (keyboard **F**) to move the cursor faster with the stick or keys.

### DLC

DLC is used when you dump it from your own copy, like the game. Put each add-on in `dlc\<title id>\romfs` of your dump folder (next
to `exefs\` and `romfs\`, or next to `base\` and `updates\`). A `.nup` with add-ons is unpacked that way on its own. The **Play**
page lists the DLC it found.

### Mods

RomFS mods go in the `mods\` folder next to the launcher (**Play** > **Open mods folder**), in the layout emulators use:
`mods\<mod name>\romfs\...`. Files in a mod replace the game's files at the same path. If two mods change the same file, the mod
whose name sorts last wins, so you can name them `01-...` and `02-...`. Rename a mod to start with `-` or `_` to turn it off.

Code mods (exefs patches) go in `mods\<mod name>\exefs\` or a folder inside it: an `.ips` / `.ips32` file named after main's
build ID (`DB93B0125F4850601D61C3EDDE012F8EB2ECFE77.ips`) or a `.pchtxt` with that `@nsobid`. Because the game code is compiled ahead of
time, they are applied when the game is built, not when it starts: after adding, changing or removing one, press **Set up** again
(the Play page says the build is out of date). Patches for the other modules (sdk, subsdk*) are not used. A mod whose patches
cannot be applied (a patch on an instruction this port hooks, outside main, or a file Set up cannot read) is left out with a note
naming it, and the other mods are still applied; the game's log says at each start which mods' patches are in the build and which
were left out. A `.pchtxt` is read as EmblemWeave reads it: lines before the first `@enabled` / `@disabled` apply, offsets may
have any number of digits and a `0x`, and bytes may be run together or two digits apart. A patch may also add code in the padding right
after main's code or rodata (a "code cave", up to the next 4 KiB page): Set up then grows that segment so the code is built in.
Removing a mod that only adds hook sites (`.sites`) builds the game again; switching it off (in the Mods menu, or with a `-` or `_`
at the start of its folder name) does not: the build keeps its sites.

Code mods made for this port can also be DLLs in the mod's `exefs\` folder (`mods\<mod name>\exefs\<mod>.dll`), so one mod folder can
carry an emulator's patches and this port's DLL side by side. They load when the game starts and can hook the game at the sites it was
built with. A DLL mod that needs hook sites of its own lists them in a `.sites` file beside it (see
[mod_api.h](runtime/include/nxrt/mod_api.h)); like exefs patches, they are added when the game is built, so press **Set up** after
installing, updating or removing such a mod (switching it on or off in the Mods menu needs only a restart; a mod whose sites
the current build lacks is named on screen at start and marked "needs Set up" in the menu). From interface version 2 a mod can also give the game functions of its own to call, which is what adding an entry to one
of the game's menus needs, and from version 3 a mod can change the game's data files as they are loaded (add rows to a table,
for example), so two mods that add to the same file no longer overwrite each other. From version 4 a mod can show a list screen in the game's own style, with a value per row that left and right change. A DLL runs with the same rights as the game: only install mods you trust. `[fs] dll_mods = false` in `ObeliskChamber.ini` turns them
off. Writing one: [runtime/include/nxrt/mod_api.h](runtime/include/nxrt/mod_api.h).

Mods can be switched on and off in the game: the title screen's DLC entry reads **DLC / Mods** and lists every mod folder on a RomFS
tab and an ExeFS tab. The switch is the file `mod.ini` in the mod's folder (`romfs = on` / `off`, `exefs = on` / `off`; no file means
on), which you can also edit by hand. RomFS files and DLLs follow it at the next start, exefs patches at the next **Set up**.

### Updating to a new release

Extract the new zip to a new folder, copy your `save\` folder (and `mods\`, if you use mods) into it, and run **Set up** again.

## Troubleshooting

- **"These game files are a different version"**: only update 131072 is supported. Check that you dumped the update, not only the
  base game.
- **Setup stopped or failed**: press **Set up** again to continue where it stopped. **Open full log** on the Setup page shows what
  went wrong.
- **The game closes or freezes**: look at `ObeliskChamber.log` in the launcher's folder, and update your GPU driver.
- **Intel GPU, crash at or after the title screen**: the log should have a `GPU quirks: [gfx] intel_compat on` line. If you copied
  `ObeliskChamber.ini` from a test build, remove the lines you added to its `[nvn]` and `[gfx]` sections: a key set by hand wins
  over the Intel defaults.
- **A short black flash, blocky clouds or slow texture pop-in after loading** in an older build: update; 0.3.0 fixes these.

## Reporting problems

Open an [issue](../../issues) with `ObeliskChamber.log`, your GPU, driver and Windows version, and the release you used. Please do not attach
game files or anything made from them.

## Building from source

See [docs/BUILDING.md](docs/BUILDING.md).

## Contributing

Contributions are welcome: open a pull request against `master`, or an [issue](../../issues) first for anything large.

- Keep game files and anything made from them (dumps, extracted assets, decompiled code) out of the repository and out of pull
  requests. Addresses, offsets and hook code are fine.
- Don't copy code from other mods or ports unless its licence allows it; credit what you build on, as the [Credits](#credits) do.
- A change to `recomp/games/fefw/main.sites` or to the code converter needs a new **Set up** to take effect; say so in the pull
  request.
- Describe what you tested and on what hardware. Releases get one person's testing on one PC, so a second setup helps.
- Code mods that don't need a change here are best shared on their own (see [Mods](#mods) and
  [mod_api.h](runtime/include/nxrt/mod_api.h)).

## Credits

This project stands on the work of others. Thank you.

**The original**
- **[fefw-recomp](https://github.com/Goldoire/fefw-recomp) by Goldoire**: the port itself, about 94% of what is here. See
  [Where this comes from](#where-this-comes-from).
- Its contributors: **Stellarznith** (DualSense haptic feedback, the F1 upscaler and frame generation status, translations),
  **Aidan Kinzett** (the Linux package and starting the game through Steam's Proton or Wine, a clearer Set up error),
  **Paulius (Kompas9)** (ultrawide fixes, torch shadows, storage-buffer reads, Vulkan layer handling), **ranbinrang-rgb** (code mods
  as DLLs) and **Abubakar-ux** (the frame rate counter with frame generation), and everyone who filed issues and helped test it.

**[Kompas9's fork](https://github.com/Kompas9/fefw-recomp) by Paulius (Kompas9)**

After the original's last release, Paulius kept improving it in a fork of their own. These changes were brought in here with their
commits, so the history shows them as the author:
- Faster game code: the condition flags as a local of each translated function instead of four fields written to memory. Later
  replaced by Stephen Jayakar's version of the same idea (below), which goes further; this change was reverted for it.
- The game's alpha test (the hardware stage that hides see-through pixels, used by a dissolve effect) applied as a shader variant
  (`[nvn] alpha_test`).
- A shader fix for integer textures (four shaders that were read as float images, issue #89 of the original).
- GPU statistics counters written once per address (`[nvn] stat_counters`), saving render-thread time each frame.
- A D-pad for the tutorial's page-turn glyphs with a PlayStation or Xbox pad (`[fefw] pager_dpad`).
- The launcher names the Streamline files in the DLSS folder that the game does not use (NVIDIA's NGX can load stray ones).
- astc-encoder 5.7.1, and much quieter logs: no unknown-NVN-function list, Streamline plugin notes, libvpx line or RGB565 texture
  warnings at every start.

**[Stephen Jayakar's fork](https://github.com/stephenjayakar/fefw-recomp) by Stephen Jayakar**

Stephen Jayakar measured and fixed the original's hitches and slow frames in a performance fork of his own (GPL-3.0). His changes
are brought in here with his commits, so the history shows him as the author, and each commit names the commit it came from:
- An import freeze finds its pages from the heap's page table instead of VirtualQuery, which held the write tracker's lock for up
  to 85 ms: the 140 to 500 ms gameplay hitches next to a memory pool import (`[memory] freeze_table`).
- The same for the commit check of a pool slab.
- The next 64 MiB pool-copy chunk is made ahead on a worker thread instead of on the render thread at area arrivals
  (`[nvn] mirror_spare`).
- A GPU untile of a first upload no longer imports a 64 MiB pool chunk on the render thread (`[nvn] untile_import`), and texture
  stand-ins encode fewer levels (`[nvn] standin_encode_px`).
- The render thread's Vulkan calls made by a second thread (`[nvn] replay_threads`; on here with 6 or more CPU cores), handed over in batches,
  and a small cache of pipeline layouts (`[nvn] layout_cache`). On his PC the busiest street's render-thread work went from 16.8 to
  about 14 ms a frame.
- Fewer GPU barriers, each behind its own setting (on here by default; full barriers on Intel): dispatches that share nothing run side by side
  (`[nvn] dispatch_barriers`), the game's own barriers kept only where they guard something (`[nvn] game_barriers`), runs of
  uniform-buffer updates with two barriers instead of two each (`[nvn] batch_ubo_updates`), feedback copies of only the sampled
  levels (`[nvn] feedback_levels`) and a read-only depth pass kept open (`[nvn] depth_ro_sticky`), with `[prof] gpu_stats` to
  measure them. On his PC at 4K the GPU time per frame went from 15.1 to 10.9 ms with all five on.
- Less CPU spent between threads: the system-call counters kept per thread instead of in shared memory every thread writes,
  imports resolved in one lookup, command lists that reserve their size up front, a cheaper starvation guard
  (`[sched] starvation_lean`) and short spins before sleeping for chosen game threads only (`[kernel] spin_threads`).
- Texture streaming for area loads: textures without their data yet are drawn with quick stand-ins while background jobs read or
  make the real data (`[nvn] tex_async = standin`; the render thread opens no cache file), the stand-ins are cheaper and smaller
  (`standin_copy`, `standin_rows`), uploads go through a persistent staging ring (`staging_ring_mb`), and an area's first uploads
  are spread over frames (`pool_upload_ms`, 24 ms a frame while a backlog streams in). On his PC the load-in frame after a menu
  went from 3.8 s to about 0.3 s.
- Fewer first-use stalls from shaders: the whole shader pack is kept in memory so no shader file is read during play
  (`[nvn] shader_pack_all`, `shader_pack_resident`), compute pipelines are built ahead on background threads
  (`compute_async = prebuild`), draws no longer wait for a pipeline still being built (`prewarm_wait = false`), two extra threads
  take urgent jobs (`gpl_burst_threads`), and the background build threads scale with the CPU. On his PC, with empty shader
  caches, the worst battle frame went from 142 to 44 ms.
- Faster translated game code (takes effect at the next Set up): the condition flags kept in a local of each function and
  tested from the compared values, native versions of the game's memcpy / memset / strlen and similar routines, the hottest
  functions packed together and small hot functions copied into their callers, the game's zstd decoder run natively (zstd 1.5.7,
  BSD licence), the translated code told that the guest context does not escape (so the compiler keeps its values in
  registers), whole-register vector writes, and temporary guest registers in locals. On his PC the game's main thread used
  about 25% less CPU time and arrival frames after fast travel dropped from 485 to about 320 ms. His profile-guided build
  options (`fefw_build.py --pgo-gen` / `--pgo-use`) are there for developers; releases do not use them.
- Long submissions handed to the GPU in parts at render pass boundaries, so the GPU starts on a frame while the render thread
  still records the rest (`[nvn] split_submit_ms`, off by default), with the GPU's busy time per frame in `[prof] gpu_stats`.

**Techniques from other ports and mods** (the idea and its findings; our own code, written here)
- The continued [fefw-recomp](https://github.com/metrist38/fefw-recomp) (metrist38) and its contributor **moistman7**: the Intel
  test matrix that found the two settings losing the GPU (`[gfx] intel_compat`), spin waits on CPUs with 8 or more cores
  (`[kernel] wait_spin`), the lifted code at `/O2`, DLSS retried after a failed frame, wide one-edge pictures kept at 16:9 on
  ultrawide, cheaper held-result checks after a program bind (`[nvn] out_prog_dirty`, `out_memo_held`), the smooth sampler that
  fixes blocky clouds (`[nvn] reserved_sampler`), compile pacing (`[nvn] compile_pace_ms`), the cached camera block
  (`[nvn] camera_patch_cached`) and full-screen pictures keeping their shape on ultrawide (`[fefw] ultrawide_images`), several of
  them through **[EmblemWeave](https://github.com/XiONDiGiTaL/EmblemWeave)** (XiØN), which also showed prompt fonts read through the
  RomFS.
- **Savage Wind Recomp** (Revivedx): scripted performance routes, DLSS model E on RTX 20 / 30 cards, a compiler barrier on polling
  loops and inline multiply-adds for the compatible build.
- **ProjectReLink**: Vulkan objects destroyed on a low-priority thread (`[nvn] reaper`). **TotK Recomp**: the profile stamp that
  warns when a runtime profile is out of date.
- The Intel players on issue #81, whose logs and test runs made the Intel fixes possible.
- **Mi-Kel** ([EmblemWeave #71](https://github.com/XiONDiGiTaL/EmblemWeave/pull/71)): the code mods' panels (the Mods window on
  F8 and the menu's Mods tab, `add_panel`, `panel_visible`, the `NxModUi` widgets). Ported from that pull request (GPL-3.0, as here)
  with the same interface, so a mod's panel works in both runtimes.

**This fork**
- **[xinthefreefallxai](https://github.com/xinthefreefallxai)**: maintainer. Chose what to build, directed the work, tested every
  change on real hardware and made the releases.
- **Claude** (Anthropic's AI model): wrote the maintainer's code changes under that direction.
- **ranbinrang-rgb**: mods that bring their own hook sites (`exefs/*.sites`, lifted by Set up), with the checks around them: a
  mod's sites are kept while it is switched off, hooks are checked against the lifted list, the Mods menu names mods that need
  Set up, and Set up warns about midhook sites where the condition flags are still live; and the same checks for a launcher run
  from a source checkout (Set up notices a mod's new sites and lifts them).
- The [Fortune's Weave decompilation project](https://code.vexcited.com/decompile/fefw): its function and class names helped find the
  game's menu code. No code from it is used.

**What the original was built on, and what both use**
- [nxrecomp](https://github.com/drakolordx7/nxrecomp) by drakolordx7: the recompiler this is built on.
- [pokopia-recomp](https://github.com/drakolordx7/pokopia-recomp) (drakolordx7) and [featureJosh/pokopia-recomp](https://github.com/featureJosh/pokopia-recomp)
  (featureJosh): the runtime, tools and launcher this project started from (GPL-3.0).
- [drakolordx7/switch2-recomp](https://github.com/drakolordx7/switch2-recomp): the launcher design this launcher follows.
- [nxnative-penis6](https://git.eden-emu.dev/penis6/nxnative-penis6) by seedds (MIT): the movie decoder front end, its dav1d copy and
  several hardware facts (notices in `runtime/THIRD_PARTY_NOTICES.md`).
- [Kompas9's Switch-Game-Patches](https://github.com/Kompas9/Switch-Game-Patches) (MIT): the Fortune's Weave ultrawide patch, whose sites and
  findings `[fefw] ultrawide` re-implements as runtime hooks.
- [Fortune's Eden](https://github.com/TheSneakyBadger/FEFW-Stable-with-60-FPS-Patch) by TheSneakyBadger (GPL-3.0-or-later), whose map
  started from Jbuck7's 60 FPS cheat: the per-frame sites and scaling rule `[fefw] frame_fixes` re-implements as runtime hooks, and the
  graphics settings files and keys behind `[fefw] reflections`, `ambient_occlusion`, `crowd_detail` and `scenery_detail`.
- [Ryujinx / Ryubing](https://git.ryujinx.app/ryubing/ryujinx) (MIT): the shader translator and tiling code; [Mesa](https://gitlab.freedesktop.org/mesa/mesa) NAK (MIT).
- SuperMarioWonderRecomp (MIT); smo-recomp, ScarletNX and ssbu-recomp (GPL-3.0); [yuzu and Eden](https://git.eden-emu.dev/eden-emu/eden)
  (GPL); libnx (ISC); LLVM libc++abi (Apache-2.0 with LLVM exceptions).
- [Hookshot](https://github.com/NotRexed/Hookshot) (GPL-3.0) and SkyboundTotk: the RomFS mod folder layout and rules followed here, so
  mods work the same in both.
- [nup](https://github.com/a55uka/nup) by a55uka: the `.nup` package format the launcher can read.
- [zbic](https://github.com/kinnay/zbic) by Yannik Marchand (GPLv2; installed by pip at setup, not included here): ZBIC decompression.
- [dav1d](https://code.videolan.org/videolan/dav1d) (BSD-2-Clause), [libvpx](https://chromium.googlesource.com/webm/libvpx) (BSD-3-Clause),
  [Zstandard](https://github.com/facebook/zstd) (BSD), [SDL3](https://github.com/libsdl-org/SDL) (zlib), volk, VulkanMemoryAllocator, Dear ImGui (MIT),
  Vulkan-Headers (Apache-2.0 or MIT).
- [astc-encoder](https://github.com/ARM-software/astc-encoder) (Apache-2.0), [bc7enc_rdo](https://github.com/richgel999/bc7enc_rdo)
  (MIT or public domain), ISPC (BSD-3-Clause).
- AMD FidelityFX / FSR SDK headers (MIT), NVIDIA Image Scaling (MIT), NVIDIA Streamline headers (MIT). No AMD or NVIDIA binaries
  are included; FSR and DLSS files are not distributed.
- [Sascha Willems' Vulkan samples](https://github.com/SaschaWillems/Vulkan) (MIT): the stand-in shader test corpus.
- [Kenney input prompts](https://kenney.nl/assets/input-prompts) (CC0), DejaVu fonts, [Tauri](https://tauri.app), [Lucide](https://lucide.dev) icons (ISC).

Licence texts: `runtime/THIRD_PARTY_NOTICES.md`, `recomp/THIRD_PARTY.md`, `tools/*/licenses/`, `tools/textures/THIRD_PARTY.md` and the
licence files inside `runtime/third_party/`. All of these licences are compatible with the GPL-3.0 this project is released under.

## License

GPL-3.0 ([LICENSE](LICENSE)). Third-party code keeps its own licence.
