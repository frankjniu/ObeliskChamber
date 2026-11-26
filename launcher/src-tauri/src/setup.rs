// SPDX-License-Identifier: GPL-3.0-or-later
//! First-run setup from a source checkout. The repository ships no game code: every step runs one of the repository's own scripts
//! on the player's PC, against the player's own dump, and writes only inside the checkout (`game/`, `work/`, build folders) and the
//! run folder. The dump itself is only read.
//!
//! Steps, in order (each is skipped when its output is already there and current, so Set up after a pause continues):
//!   python   tools/.venv with recomp/requirements.txt (uv)
//!   exefs    game/dump/exefs: the dump's NSOs, `main` written uncompressed (tools/nso/prepare_exefs.py)
//!   lift     work/build/fefw/fefw_*.lib: lift and compile the six modules (recomp/scripts/build_game.cmd fefw), main with main.sites
//!            plus the installed mods' exefs/*.sites (lift_sites_file); not done while a mod has a site the build lacks
//!   textures tools/textures/third_party: astc-encoder and bc7enc sources for the ASTC to BC7 transcoder (fetch_third_party.ps1)
//!   exe      game-build/fefw/build/release/ObeliskChamber.exe (game-build/scripts/build_fefw.ps1)
//!   harness  the shader translator (tools/shaders: Ryujinx sources staged, dotnet build)
//!   shaders  work/shaders/spirv: shaders found in the romfs and main, translated to SPIR-V
//!   ini      ObeliskChamber.ini in the run folder (settings.rs)
//! Pause kills the running step's process tree; the build tools keep finished work (ninja, the harness's results.tsv).

use serde::Serialize;
use std::collections::VecDeque;
use std::fs;
use std::io::{BufRead, BufReader, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

pub const STEPS: [(&str, &str); 8] = [
    ("python", "Python environment"),
    ("exefs", "Game code from your dump"),
    ("lift", "Recompile the game code"),
    ("textures", "Texture transcoder sources"),
    ("exe", "Build ObeliskChamber.exe"),
    ("harness", "Shader translator"),
    ("shaders", "Translate the shaders"),
    ("ini", "Write ObeliskChamber.ini"),
];

/// The steps of the release package (no toolchain on the player's PC: the bundled Python and Zig build ObeliskChamber.exe).
pub const PACKAGE_STEPS: [(&str, &str); 3] = [
    ("build", "Recompile the game code from your dump"),
    ("shaders", "Translate the shaders"),
    ("ini", "Write ObeliskChamber.ini"),
];

/// Every path the setup and Play use, derived from the checkout (or the release package), the dump and the run folder.
#[derive(Clone)]
pub struct Paths {
    /// the checkout; in the release package, the install folder
    pub repo: PathBuf,
    pub exefs: PathBuf,
    pub romfs: PathBuf,
    /// the dump's dlc/ folder (add-on content the player dumped), when it holds any
    pub dlc: Option<PathBuf>,
    pub game_dir: PathBuf,
    /// the release package's tools/ folder (tools/build/fefw_build.py next to the launcher); None in a checkout
    pub package: Option<PathBuf>,
    /// Set up's "Experimental: decompiled lighting shader" box (the launcher's own config): after the shaders are translated,
    /// tools/shaders/lighting_edit.py makes an edited copy of the lighting shader that the runtime uses ([nvn] edited_shaders)
    pub lighting_edit: bool,
}

impl Paths {
    fn in_game(&self, rel: &[&str]) -> PathBuf {
        rel.iter().fold(self.game_dir.clone(), |p, r| p.join(r))
    }
    pub fn python(&self) -> PathBuf {
        match &self.package {
            Some(t) => package_python(t),
            None => self.repo.join("tools").join(".venv").join("Scripts").join("python.exe"),
        }
    }
    pub fn plain_exefs(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["exefs"]),
            None => self.repo.join("game").join("dump").join("exefs"),
        }
    }
    /// RomFS mods ([fs] mods): <run folder>/mods/<name>/romfs
    pub fn mods(&self) -> PathBuf {
        self.in_game(&["mods"])
    }
    pub fn libs(&self) -> PathBuf {
        self.repo.join("work").join("build").join("fefw")
    }
    pub fn exe(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["ObeliskChamber.exe"]),
            None => exe_path(&self.repo),
        }
    }
    pub fn harness(&self) -> PathBuf {
        match &self.package {
            Some(t) => t.join("spirv_harness").join(if cfg!(windows) { "spirv_harness.exe" } else { "spirv_harness" }),
            None => self.repo.join("tools").join("shaders").join("build").join("bin").join("Release").join("net10.0").join("spirv_harness.dll"),
        }
    }
    pub fn extractor(&self) -> PathBuf {
        match &self.package {
            Some(t) => t.join("shaders").join("extract_corpus.py"),
            None => self.repo.join("tools").join("shaders").join("ampere").join("extract_corpus.py"),
        }
    }
    /// tools/shaders/lighting_edit.py (the package has it next to the extractor, with spirv-cross and glslang in shaders/bin)
    pub fn lighting_tool(&self) -> PathBuf {
        match &self.package {
            Some(t) => t.join("shaders").join("lighting_edit.py"),
            None => self.repo.join("tools").join("shaders").join("lighting_edit.py"),
        }
    }
    /// The edited modules, beside the translated ones: the runtime reads <shader cache>/../edited
    pub fn edited(&self) -> PathBuf {
        let spirv = self.spirv();
        spirv.parent().map(|d| d.join("edited")).unwrap_or_else(|| spirv.join("edited"))
    }
    pub fn corpus(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "corpus"]),
            None => self.repo.join("work").join("shaders").join("corpus"),
        }
    }
    pub fn spirv(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "spirv"]),
            None => self.repo.join("work").join("shaders").join("spirv"),
        }
    }
    pub fn bc7(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "bc7"]),
            None => self.repo.join("work").join("bc7_cache"),
        }
    }
    fn shader_stamp(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["shader_cache", "setup.json"]),
            None => self.repo.join("work").join("shaders").join("setup.json"),
        }
    }
    pub fn log(&self) -> PathBuf {
        match &self.package {
            Some(_) => self.in_game(&["setup.log"]),
            None => self.repo.join("work").join("setup.log"),
        }
    }
}

// ---- hook sites of mods (mods/<name>/exefs/*.sites; game-build/release/fefw_build.py collect_mod_sites) ------------------------------
// A code mod can hook only an instruction main was lifted with as a site; a mod that needs its own lists them in exefs/*.sites. The
// release package's build stamp covers them (fefw_build.py --check). In a checkout, Set up lifts main with main.sites plus these
// sites (FEFW_SITES, recomp/games/fefw/CMakeLists.txt), and the lift step is not done while a mod has a site the build lacks.

const MOD_SITE_KINDS: [&str; 3] = ["inline", "midhook", "entryhook"];

/// A site list: its @nsobid (upper case, "" when it has none) and its (kind, address) lines.
fn read_site_list(path: &Path) -> Option<(String, Vec<(String, u64)>)> {
    let text = fs::read_to_string(path).ok()?;
    let mut bid = String::new();
    let mut sites = Vec::new();
    for line in text.lines() {
        let line = line.split('#').next().unwrap_or("");
        let mut t = line.split_whitespace();
        let (Some(kind), Some(arg)) = (t.next(), t.next()) else { continue };
        if kind.eq_ignore_ascii_case("@nsobid") {
            bid = arg.to_ascii_uppercase();
        } else if let Ok(a) = u64::from_str_radix(arg.trim_start_matches("0x").trim_start_matches("0X"), 16) {
            sites.push((kind.to_string(), a));
        }
    }
    Some((bid, sites))
}

/// A mod's build ID against main's: a prefix of it, the rest zeros (as fefw_build.py bid_matches and the runtime's same_build).
fn same_build(mod_bid: &str, main_bid: &str) -> bool {
    if main_bid.is_empty() {
        return true;
    }
    let mut full = main_bid.to_ascii_uppercase();
    while full.len() < 64 {
        full.push('0');
    }
    let t = mod_bid.to_ascii_uppercase();
    t.len() >= 16 && t.len() <= 64 && full.starts_with(&t) && full[t.len()..].chars().all(|c| c == '0')
}

/// main's build ID (NSO header +0x40, 32 bytes) as hex and its code (.text memory offset and size, header +0x14 / +0x18); None
/// when the file cannot be read.
fn main_header(p: &Paths) -> Option<(String, u64, u64)> {
    let mut head = [0u8; 0x60];
    fs::File::open(p.plain_exefs().join("main")).and_then(|mut f| f.read_exact(&mut head)).ok()?;
    let word = |o: usize| u32::from_le_bytes([head[o], head[o + 1], head[o + 2], head[o + 3]]) as u64;
    Some((head[0x40..0x60].iter().map(|b| format!("{b:02X}")).collect(), word(0x14), word(0x18)))
}

/// One site a mod adds: `<mod>/exefs/<file>:<line>`, kind, guest address.
type ModSite = (String, String, u64);

const MAIN_BASE: u64 = 0x71_0000_0000;
const NEXT_MODULE: u64 = 0x75_0000_0000;

/// The installed mods' sites for this game build, checked as game-build/release/fefw_build.py collect_mod_sites and write_sites
/// check them, with the same messages: a kind a mod cannot add, a line that is not `<kind> <hex address>`, a file without
/// @nsobid, an address that is not an instruction in main's code. The first problem is the error. A mod's exefs/ folder counts
/// whether or not its mod.ini switches exefs off (fefw_build.py keeps those sites too, so switching a mod needs no Set up).
fn mod_sites(p: &Paths) -> Result<Vec<ModSite>, String> {
    let header = main_header(p);
    let bid = header.as_ref().map(|h| h.0.clone()).unwrap_or_default();
    let mut out = Vec::new();
    let Ok(rd) = fs::read_dir(p.mods()) else { return Ok(out) };
    let mut dirs: Vec<PathBuf> = rd.flatten().map(|e| e.path()).filter(|d| d.join("exefs").is_dir()).collect();
    dirs.sort_by_key(|d| d.file_name().map(|n| n.to_string_lossy().to_lowercase()));
    for d in dirs {
        let name = d.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
        let Ok(files) = fs::read_dir(d.join("exefs")) else { continue };
        let mut files: Vec<PathBuf> = files
            .flatten()
            .map(|e| e.path())
            .filter(|f| f.is_file() && f.extension().map_or(false, |x| x.eq_ignore_ascii_case("sites")))
            .collect();
        files.sort_by_key(|f| f.file_name().map(|n| n.to_string_lossy().to_lowercase()));
        for f in files {
            let file = f.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            let where_ = format!("{name}/exefs/{file}");
            let Ok(text) = fs::read_to_string(&f) else { continue };
            let mut mbid: Option<String> = None;
            let mut recs = Vec::new();
            for (i, line) in text.trim_start_matches('\u{feff}').lines().enumerate() {
                let ln = i + 1;
                let t: Vec<&str> = line.split('#').next().unwrap_or("").split_whitespace().collect();
                if t.is_empty() {
                    continue;
                }
                if t[0].eq_ignore_ascii_case("@nsobid") {
                    if t.len() != 2 {
                        return Err(format!("{where_}:{ln}: @nsobid takes one build ID"));
                    }
                    mbid = Some(t[1].to_string());
                    continue;
                }
                if !MOD_SITE_KINDS.contains(&t[0]) {
                    // a kind another port defines, or a later one: skipped, as the release script does (fefw-recomp issue #165)
                    continue;
                }
                let addr = (t.len() == 2)
                    .then(|| u64::from_str_radix(t[1].trim_start_matches("0x").trim_start_matches("0X"), 16).ok())
                    .flatten()
                    .ok_or_else(|| format!("{where_}:{ln}: expected \"<kind> <guest address in hex>\""))?;
                recs.push((ln, t[0].to_string(), addr));
            }
            let Some(mbid) = mbid else {
                return Err(format!("{where_} has no @nsobid line (the build ID of the main its sites are for)"));
            };
            if !same_build(&mbid, &bid) {
                continue; // another version of the game
            }
            for (ln, kind, addr) in recs {
                if addr % 4 != 0 || !(MAIN_BASE..NEXT_MODULE).contains(&addr) {
                    return Err(format!("{where_}:{ln}: 0x{addr:x} is not an instruction address in main (main is at 0x{MAIN_BASE:x})"));
                }
                if let Some((_, text_off, text_size)) = &header {
                    let off = addr - MAIN_BASE;
                    if !(*text_off..text_off + text_size).contains(&off) {
                        return Err(format!("{where_}:{ln}: 0x{addr:x} is outside main's code"));
                    }
                }
                out.push((format!("{where_}:{ln}"), kind, addr));
            }
        }
    }
    Ok(out)
}

/// The mod folder of a site's `<mod>/exefs/<file>:<line>`.
fn site_mod(where_: &str) -> &str {
    where_.split('/').next().unwrap_or(where_)
}

/// Mods with a hook site that the lifted module libraries lack (work/build/fefw/main.sites). Empty when
/// all mod sites are present in main.sites, or when a .sites file has an error (reported by mod_sites_problem).
/// When main.sites does not exist yet (before the first lift), compares against the base recomp/games/fefw/main.sites.
pub fn mods_missing_lift_sites(p: &Paths) -> Vec<String> {
    let site_file = p.libs().join("main.sites");
    let base_file = p.repo.join("recomp").join("games").join("fefw").join("main.sites");
    let check_file = if site_file.is_file() { &site_file } else { &base_file };
    let Some((_, lifted)) = read_site_list(check_file) else { return vec![] };
    let lifted: std::collections::HashSet<(String, u64)> = lifted.into_iter().collect();
    let mut names: Vec<String> = Vec::new();
    for (where_, kind, a) in mod_sites(p).unwrap_or_default() {
        let name = site_mod(&where_).to_string();
        if !lifted.contains(&(kind, a)) && !names.contains(&name) {
            names.push(name);
        }
    }
    names
}

/// Mods with a hook site that the built ObeliskChamber.exe was not lifted with (its ObeliskChamber.sites next to it). Empty when the
/// exe or its list is not there yet (the lift / exe steps are not done then anyway), or when a .sites file has an error (reported
/// by mod_sites_problem instead).
pub fn mods_missing_sites(p: &Paths) -> Vec<String> {
    let Some((_, lifted)) = read_site_list(&p.exe().with_extension("sites")) else { return vec![] };
    let lifted: std::collections::HashSet<(String, u64)> = lifted.into_iter().collect();
    let mut names: Vec<String> = Vec::new();
    for (where_, kind, a) in mod_sites(p).unwrap_or_default() {
        let name = site_mod(&where_).to_string();
        if !lifted.contains(&(kind, a)) && !names.contains(&name) {
            names.push(name);
        }
    }
    names
}

/// A problem in the mods' .sites files (the first one), as the release script reports it.
fn mod_sites_problem(p: &Paths) -> Option<String> {
    mod_sites(p).err()
}

/// Checkout: the site list the lift uses, main.sites plus the mods' sites (written only when it changes, since the lift depends on
/// the file). Without mod sites it is main.sites itself. A problem in a .sites file stops Set up with its message.
fn lift_sites_file(p: &Paths) -> Result<PathBuf, String> {
    let own_path = p.repo.join("recomp").join("games").join("fefw").join("main.sites");
    let extra = mod_sites(p)?;
    if extra.is_empty() {
        return Ok(own_path);
    }
    let own = fs::read_to_string(&own_path).map_err(|e| format!("cannot read {}: {e}", own_path.display()))?;
    let have: std::collections::HashSet<(String, u64)> = read_site_list(&own_path).map(|(_, s)| s).unwrap_or_default().into_iter().collect();
    let mut text = own.trim_end().to_string() + "\n\n# ---- added by mods (exefs/*.sites)\n";
    let mut seen = std::collections::HashSet::new();
    for (where_, kind, a) in extra {
        if have.contains(&(kind.clone(), a)) || !seen.insert((kind.clone(), a)) {
            continue;
        }
        text += &format!("{kind} 0x{a:x}   # {where_}\n");
    }
    let out = p.libs().join("main_with_mods.sites");
    if fs::read_to_string(&out).ok().as_deref() != Some(text.as_str()) {
        fs::create_dir_all(p.libs()).map_err(|e| e.to_string())?;
        fs::write(&out, text).map_err(|e| format!("cannot write {}: {e}", out.display()))?;
    }
    Ok(out)
}

/// The mods' exefs files (names, sizes, times): part of the release package's check key, so installing, updating or removing a code
/// mod while the launcher runs asks fefw_build.py --check again.
fn mods_exefs_stamp(p: &Paths) -> u64 {
    use std::hash::{Hash, Hasher};
    let mut h = std::collections::hash_map::DefaultHasher::new();
    if let Ok(rd) = fs::read_dir(p.mods()) {
        let mut files: Vec<(PathBuf, u64, Option<std::time::SystemTime>)> = Vec::new();
        for d in rd.flatten() {
            for f in fs::read_dir(d.path().join("exefs")).into_iter().flatten().flatten() {
                if let Ok(m) = f.metadata() {
                    files.push((f.path(), m.len(), m.modified().ok()));
                }
            }
        }
        files.sort();
        files.hash(&mut h);
    }
    h.finish()
}

/// The last `fefw_build.py --check`: (done.json mtime and size, ObeliskChamber.exe mtime, the mods' exefs files) -> current. The tools
/// do not change while the launcher runs and a finished build rewrites done.json, so one Python run per finished build and mod set is
/// enough.
type CheckKey = (Option<std::time::SystemTime>, u64, Option<std::time::SystemTime>, u64);
static BUILD_CHECK: Mutex<Option<(CheckKey, bool)>> = Mutex::new(None);

fn package_build_ok(p: &Paths) -> bool {
    let Some(tools) = &p.package else { return false };
    let done = p.in_game(&["build", "done.json"]);
    let (Ok(dm), true) = (fs::metadata(&done), p.exe().is_file()) else { return false };
    let key: CheckKey = (dm.modified().ok(), dm.len(), mtime(&p.exe()), mods_exefs_stamp(p));
    if let Some((k, b)) = BUILD_CHECK.lock().unwrap().as_ref() {
        if *k == key {
            return *b;
        }
    }
    let mut cmd = Command::new(p.python());
    cmd.arg("-I").arg(tools.join("build").join("fefw_build.py")).arg("--check").arg("--game").arg(&p.game_dir);
    // the build for this processor (cpu.rs): an ObeliskChamber.exe built for the other one is not current, so Set up rebuilds it
    if let Some(v) = crate::cpu::build_variant() {
        cmd.args(["--cpu", v]);
    }
    quiet(&mut cmd);
    let ok = cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
    *BUILD_CHECK.lock().unwrap() = Some((key, ok));
    ok
}

/// The release package's Python: tools/python/python.exe (the embeddable package) on Windows. On Linux tools/python/bin/python3
/// (a relocatable build) when the package has one, else the system's python3, which then needs the lifter's packages (zbic, lz4,
/// capstone, zstandard) installed.
pub(crate) fn package_python(tools: &Path) -> PathBuf {
    if cfg!(windows) {
        return tools.join("python").join("python.exe");
    }
    let bundled = tools.join("python").join("bin").join("python3");
    if bundled.is_file() {
        return bundled;
    }
    which("python3").unwrap_or(bundled)
}

pub fn exe_path(repo: &Path) -> PathBuf {
    repo.join("game-build").join("fefw").join("build").join("release").join("ObeliskChamber.exe")
}

pub const MODULES: [&str; 6] = ["main", "sdk", "subsdk0", "subsdk1", "subsdk2", "rtld"];

#[derive(Serialize, Clone, Default)]
pub struct StepState {
    pub id: String,
    pub title: String,
    /// "done", "todo", "running", "error"
    pub state: String,
    pub detail: String,
}

#[derive(Serialize, Clone, Default)]
pub struct Progress {
    /// "idle", "running", "done", "cancelled", "error"
    pub state: String,
    pub step: String,
    pub done: u64,
    pub total: u64,
    pub message: String,
}

/// The running job's state, shared with the page.
#[derive(Default)]
pub struct Shared {
    pub progress: Mutex<Progress>,
    pub log: Mutex<VecDeque<String>>,
    pub cancel: AtomicBool,
    pub pid: AtomicU32,
}

const LOG_LINES: usize = 600;

fn mtime(p: &Path) -> Option<std::time::SystemTime> {
    fs::metadata(p).ok().and_then(|m| m.modified().ok())
}

/// NSO flags (0x0C) of a file, None when it is not a readable NSO.
fn nso_flags(p: &Path) -> Option<u32> {
    let mut h = [0u8; 16];
    fs::File::open(p).ok()?.read_exact(&mut h).ok()?;
    (&h[..4] == b"NSO0").then(|| u32::from_le_bytes([h[12], h[13], h[14], h[15]]))
}

fn python_ok(p: &Paths) -> bool {
    if !p.python().is_file() {
        return false;
    }
    let mut cmd = Command::new(p.python());
    cmd.args(["-I", "-c", "import zbic, lz4, capstone, unicorn, elftools, zstandard"]);
    quiet(&mut cmd);
    cmd.stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false)
}

fn exefs_ok(p: &Paths) -> bool {
    let dir = p.plain_exefs();
    let stamp = fs::read_to_string(dir.join("source.txt")).unwrap_or_default();
    MODULES.iter().all(|m| dir.join(m).is_file())
        && nso_flags(&dir.join("main")).map_or(false, |f| f & 0x87 == 0)
        && stamp.lines().next().map(|l| l.trim()) == Some(crate::dump::fwd(&p.exefs).as_str())
}

fn libs_ok(p: &Paths) -> bool {
    let main = mtime(&p.plain_exefs().join("main"));
    MODULES.iter().all(|m| {
        let lib = p.libs().join(format!("fefw_{m}.lib"));
        lib.is_file() && mtime(&lib) >= main
    })
}

fn exe_ok(p: &Paths) -> bool {
    let exe = mtime(&p.exe());
    exe.is_some()
        && MODULES.iter().all(|m| mtime(&p.libs().join(format!("fefw_{m}.lib"))) <= exe)
        && mods_missing_sites(p).is_empty()
}

fn textures_ok(p: &Paths) -> bool {
    p.repo.join("tools").join("textures").join("third_party").join("astc-encoder").join("Source").join("astcenc.h").is_file()
}

const SHADER_TRANSLATOR_VERSION: u32 = 4; // 4: integer abs without SAbs, which hangs NVIDIA GPUs (#121); 3: real derivatives for the NVN2 shuffle pattern (#29 eyes); 2: portable 32-lane warps (game-build/release/fefw_build.py has the same number)

/// U1 (opt-in, for Intel testers): `[setup] intel_shaders = true` in ObeliskChamber.ini translates the shaders as Ryujinx does for
/// Intel GPUs: 64-bit float math lowered to 32-bit and no barriers in divergent branches (upstream #45, for issue #81). Changing it
/// makes the next Set up translate every shader again (the stamp records it).
fn intel_shaders(p: &Paths) -> bool {
    let ini = crate::ini::Ini::parse(&fs::read_to_string(crate::settings::game_ini(&p.game_dir)).unwrap_or_default());
    matches!(ini.get("setup", "intel_shaders").map(|v| v.trim().to_ascii_lowercase()).as_deref(), Some("true" | "1" | "on" | "yes"))
}

fn shaders_ok(p: &Paths) -> bool {
    let stamp = fs::read_to_string(p.shader_stamp()).unwrap_or_default();
    let v: serde_json::Value = serde_json::from_str(&stamp).unwrap_or_default();
    p.spirv().join("results.tsv").is_file()
        && v["complete"].as_bool() == Some(true)
        && v["translator_version"].as_u64() == Some(SHADER_TRANSLATOR_VERSION as u64)
        && v["romfs"].as_str() == Some(crate::dump::fwd(&p.romfs).as_str())
        && v["intel_shaders"].as_bool().unwrap_or(false) == intel_shaders(p)
}

/// What the edited lighting shader was made from: the tool and the translated shaders. A stamp that differs (a new launcher, shaders
/// translated again) makes the next Set up run the tool again.
fn lighting_marker(p: &Paths) -> serde_json::Value {
    serde_json::json!({ "tool": file_stamp(&p.lighting_tool()), "shaders": file_stamp(&p.spirv().join("results.tsv")) })
}

fn lighting_stamp(p: &Paths) -> PathBuf {
    p.edited().join("setup.json")
}

/// The box is ticked and the tool ran on these shaders, or the box is clear and no edited module is left.
fn lighting_ok(p: &Paths) -> bool {
    if p.lighting_edit {
        let have: serde_json::Value = serde_json::from_str(&fs::read_to_string(lighting_stamp(p)).unwrap_or_default()).unwrap_or_default();
        have == lighting_marker(p)
    } else {
        !edited_modules(p).iter().any(|f| f.is_file())
    }
}

fn edited_modules(p: &Paths) -> Vec<PathBuf> {
    fs::read_dir(p.edited())
        .map(|d| d.filter_map(|e| e.ok()).map(|e| e.path()).filter(|f| f.extension().and_then(|x| x.to_str()) == Some("spv")).collect())
        .unwrap_or_default()
}

/// Set up's optional step (experimental, off unless the box on the Set up page is ticked). It never stops Set up: when the tool
/// cannot run or leaves the shader alone, the log says so and the game uses the translated shader.
fn lighting_step(r: &Runner, p: &Paths, cwd: &Path) -> Result<bool, String> {
    if lighting_ok(p) {
        return Ok(true);
    }
    if !p.lighting_edit {
        for f in edited_modules(p) {
            let _ = fs::remove_file(f);
        }
        let _ = fs::remove_file(lighting_stamp(p));
        let _ = fs::remove_file(p.edited().join("edit.json"));
        r.log(vec!["Decompiled lighting shader: the box is not ticked, so the edited shader was removed. The game uses the translated shader.".to_string()]);
        return Ok(true);
    }
    if !p.lighting_tool().is_file() {
        r.log(vec![format!("Decompiled lighting shader: {} is missing, nothing was changed.", crate::dump::fwd(&p.lighting_tool()))]);
    } else {
        let mut c = Command::new(p.python());
        c.arg("-I").arg(p.lighting_tool()).arg("--spirv").arg(p.spirv()).arg("--out").arg(p.edited());
        match r.run("shaders", "Editing the lighting shader (experimental)", c, cwd) {
            Ok(true) => {}
            Ok(false) => return Ok(false),
            Err(e) => r.log(vec![format!("Decompiled lighting shader: the tool did not finish ({}). The game uses the translated shader.", e.lines().next().unwrap_or(""))]),
        }
    }
    let _ = fs::create_dir_all(p.edited());
    fs::write(lighting_stamp(p), lighting_marker(p).to_string()).map_err(|e| e.to_string())?;
    Ok(true)
}

/// What is done already, step by step (the page's checklist).
pub fn steps(p: &Paths, running: Option<&str>) -> Vec<StepState> {
    let list: &[(&str, &str)] = if p.package.is_some() { &PACKAGE_STEPS } else { &STEPS };
    let done = |id: &str| match id {
        "build" => package_build_ok(p),
        "python" => python_ok(p),
        "exefs" => exefs_ok(p),
        "lift" => libs_ok(p) && mod_sites_problem(p).is_none() && mods_missing_lift_sites(p).is_empty(),
        "exe" => exe_ok(p),
        "textures" => textures_ok(p),
        "harness" => p.harness().is_file() && shaders_ok(p),
        "shaders" => shaders_ok(p) && lighting_ok(p),
        "ini" => crate::settings::play_paths_ok(&p.game_dir, p),
        _ => false,
    };
    list
        .iter()
        .map(|(id, title)| StepState {
            id: id.to_string(),
            title: title.to_string(),
            state: if running == Some(*id) { "running".into() } else if done(id) { "done".into() } else { "todo".into() },
            detail: if *id == "lift" && p.package.is_none() { missing_sites_detail(p) } else { String::new() },
        })
        .collect()
}

/// The lift step's note: a problem in a mod's .sites file, or the mods with hook sites the build lacks.
fn missing_sites_detail(p: &Paths) -> String {
    if let Some(problem) = mod_sites_problem(p) {
        return problem;
    }
    let names = mods_missing_lift_sites(p);
    if names.is_empty() {
        String::new()
    } else {
        format!("new hook sites from {}: Set up lifts the game with them", names.join(", "))
    }
}

pub fn complete(p: &Paths) -> bool {
    steps(p, None).iter().all(|s| s.state == "done")
}

/// A program started from the AppImage gets the host's environment, not the AppImage's: its AppRun points PYTHONHOME, GTK_*,
/// GIO/GST/QT module paths and more into its temporary mount, which breaks Python programs (umu-run, Proton's script) and anything
/// that outlives the launcher (Steam started again by Add to Steam). Variables naming the mount lose those entries (and go when
/// nothing is left); the AppImage's own variables go. Nothing changes outside an AppImage.
pub(crate) fn host_env(cmd: &mut Command) {
    if !cfg!(target_os = "linux") {
        return;
    }
    let vars: Vec<(String, String)> = std::env::vars_os().filter_map(|(k, v)| Some((k.into_string().ok()?, v.into_string().ok()?))).collect();
    for (k, v) in appimage_fixes(&vars) {
        match v {
            Some(v) => cmd.env(k, v),
            None => cmd.env_remove(k),
        };
    }
}

/// What host_env changes in `vars` (an environment with an AppImage's APPDIR in it): (name, new value), None removes it.
pub(crate) fn appimage_fixes(vars: &[(String, String)]) -> Vec<(String, Option<String>)> {
    // only an AppImage's AppRun: on Windows (';' lists) or with an APPDIR some other program set, nothing is touched
    if !cfg!(target_os = "linux") || !vars.iter().any(|(k, v)| k == "APPIMAGE" && !v.is_empty()) {
        return vec![];
    }
    let Some(appdir) = vars.iter().find(|(k, _)| k == "APPDIR").map(|(_, d)| d.trim_end_matches('/').to_string()).filter(|d| d.len() > 1) else {
        return vec![];
    };
    let mut out = vec![];
    for (k, v) in vars {
        if matches!(k.as_str(), "APPDIR" | "APPIMAGE" | "ARGV0" | "OWD" | "PYTHONHOME" | "PYTHONPATH" | "PYTHONDONTWRITEBYTECODE" | "PERLLIB") {
            out.push((k.clone(), None));
        } else if v.contains(&appdir) {
            let kept: Vec<&str> = v.split(':').filter(|p| !p.is_empty() && !p.contains(&appdir)).collect();
            out.push((k.clone(), (!kept.is_empty()).then(|| kept.join(":"))));
        }
    }
    out
}

pub(crate) fn quiet(cmd: &mut Command) {
    host_env(cmd);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        // CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS (the compilers it starts inherit it)
        cmd.creation_flags(0x0800_0000 | 0x0000_4000);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        // its own process group, so kill_tree ends everything it starts (the lifter's workers, the compilers)
        cmd.process_group(0);
    }
}

/// Ends a process and everything it started (cmake, ninja, the compilers, the lifter's workers).
pub fn kill_tree(pid: u32) {
    if pid == 0 {
        return;
    }
    #[cfg(windows)]
    {
        let mut cmd = Command::new("taskkill");
        cmd.args(["/T", "/F", "/PID", &pid.to_string()]);
        quiet(&mut cmd);
        let _ = cmd.stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
    #[cfg(unix)]
    {
        // quiet() made the process a group leader: a negative pid signals the whole group
        let _ = Command::new("kill").args(["-KILL", "--", &format!("-{pid}")]).stdout(Stdio::null()).stderr(Stdio::null()).status();
    }
}

/// ninja's "[12/345] ..." prefix.
fn ninja_progress(line: &str) -> Option<(u64, u64)> {
    let rest = line.strip_prefix('[')?;
    let (nums, _) = rest.split_once(']')?;
    let (a, b) = nums.split_once('/')?;
    Some((a.trim().parse().ok()?, b.trim().parse().ok()?))
}

/// "scan: 12 345" (extract_corpus.py --progress) or the harness's "done N/M" style lines.
fn tool_progress(line: &str) -> Option<(u64, u64)> {
    if let Some(rest) = line.strip_prefix("@progress ") {
        let mut it = rest.split_whitespace().skip(1);
        return Some((it.next()?.parse().ok()?, it.next()?.parse().ok()?));
    }
    if let Some(rest) = line.strip_prefix("scan: ") {
        let mut it = rest.split_whitespace();
        return Some((it.next()?.parse().ok()?, it.next()?.parse().ok()?));
    }
    ninja_progress(line)
}

pub struct Runner<'a> {
    pub shared: &'a Arc<Shared>,
    pub emit: &'a (dyn Fn(Progress) + Sync),
    pub emit_log: &'a (dyn Fn(Vec<String>) + Sync),
    pub log_file: Mutex<Option<fs::File>>,
    pub log_path: PathBuf,
}

impl Runner<'_> {
    fn log(&self, lines: Vec<String>) {
        if lines.is_empty() {
            return;
        }
        {
            let mut q = self.shared.log.lock().unwrap();
            for l in &lines {
                q.push_back(l.clone());
                while q.len() > LOG_LINES {
                    q.pop_front();
                }
            }
        }
        if let Some(f) = self.log_file.lock().unwrap().as_mut() {
            use std::io::Write;
            for l in &lines {
                let _ = writeln!(f, "{l}");
            }
        }
        (self.emit_log)(lines);
    }

    fn progress(&self, step: &str, done: u64, total: u64, message: &str) {
        let p = Progress { state: "running".into(), step: step.into(), done, total, message: message.into() };
        *self.shared.progress.lock().unwrap() = p.clone();
        (self.emit)(p);
    }

    /// Runs one command to the end, streaming its output into the log. Ok(true) = success, Ok(false) = paused.
    fn run(&self, step: &str, message: &str, mut cmd: Command, cwd: &Path) -> Result<bool, String> {
        if self.shared.cancel.load(Ordering::SeqCst) {
            return Ok(false);
        }
        self.progress(step, 0, 0, message);
        self.log(vec![format!("> {:?}", cmd)]);
        cmd.current_dir(cwd);
        cmd.env("PYTHONUNBUFFERED", "1").env("NINJA_STATUS", "[%f/%t] ");
        quiet(&mut cmd);
        let mut child = cmd
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .map_err(|e| format!("could not start {:?}: {}", cmd.get_program(), e))?;
        self.shared.pid.store(child.id(), Ordering::SeqCst);
        let (tx, rx) = std::sync::mpsc::channel::<String>();
        let tx2 = tx.clone();
        let out = child.stdout.take().unwrap();
        let err = child.stderr.take().unwrap();
        let read = |r: Box<dyn Read + Send>, tx: std::sync::mpsc::Sender<String>| {
            std::thread::spawn(move || {
                for l in BufReader::new(r).split(b'\n').map_while(Result::ok) {
                    let s = String::from_utf8_lossy(&l).trim_end_matches('\r').to_string();
                    if tx.send(s).is_err() {
                        break;
                    }
                }
            })
        };
        let t1 = read(Box::new(out), tx);
        let t2 = read(Box::new(err), tx2);
        let mut batch = Vec::new();
        let mut script_error: Option<String> = None;
        let mut last = std::time::Instant::now();
        loop {
            if self.shared.cancel.load(Ordering::SeqCst) {
                kill_tree(child.id());
            }
            match rx.recv_timeout(Duration::from_millis(150)) {
                Ok(l) => {
                    if let Some((d, t)) = tool_progress(&l) {
                        let text = l.strip_prefix("@progress ").and_then(|r| r.splitn(4, ' ').nth(3)).unwrap_or(message).to_string();
                        self.progress(step, d, t, &text);
                    }
                    if let Some(e) = l.strip_prefix("@error ") {
                        script_error = Some(e.replace("\\n", "\n"));
                    }
                    batch.push(l);
                }
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {}
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => break,
            }
            if !batch.is_empty() && (last.elapsed() > Duration::from_millis(250) || batch.len() > 200) {
                self.log(std::mem::take(&mut batch));
                last = std::time::Instant::now();
            }
        }
        self.log(batch);
        let _ = t1.join();
        let _ = t2.join();
        let st = child.wait().map_err(|e| e.to_string())?;
        self.shared.pid.store(0, Ordering::SeqCst);
        if self.shared.cancel.load(Ordering::SeqCst) {
            return Ok(false);
        }
        if st.success() {
            Ok(true)
        } else if st.code() == Some(2) && script_error.is_none() && step == "build" {
            Ok(false)
        } else if let Some(e) = script_error {
            Err(e)
        } else {
            Err(format!("{} failed ({}). The last lines of the log say why; the full log is {}.", message, st, crate::dump::fwd(&self.log_path)))
        }
    }
}

fn which(exe: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path).map(|d| d.join(exe)).find(|p| p.is_file())
}

/// A tool on PATH or in one of its usual install folders.
fn find_tool(names: &[&str], extra: &[PathBuf]) -> Option<PathBuf> {
    names.iter().find_map(|n| which(n)).or_else(|| extra.iter().find(|p| p.is_file()).cloned())
}

fn local_appdata(rel: &str) -> PathBuf {
    PathBuf::from(std::env::var("LOCALAPPDATA").unwrap_or_default()).join(rel)
}

pub fn powershell() -> PathBuf {
    find_tool(&["pwsh.exe"], &[PathBuf::from(r"C:\Program Files\PowerShell\7\pwsh.exe")]).unwrap_or_else(|| PathBuf::from("powershell.exe"))
}

/// One build prerequisite and whether it was found (the page lists them before Set up).
#[derive(Serialize, Clone)]
pub struct Tool {
    pub name: String,
    pub ok: bool,
    pub found: String,
    pub hint: String,
}

pub fn check_tools(package: bool) -> Vec<Tool> {
    if package {
        return vec![]; // everything the release package needs is in it
    }
    let pf = |rel: &str| PathBuf::from(std::env::var("ProgramFiles").unwrap_or_else(|_| r"C:\Program Files".into())).join(rel);
    let pf86 = |rel: &str| PathBuf::from(std::env::var("ProgramFiles(x86)").unwrap_or_else(|_| r"C:\Program Files (x86)".into())).join(rel);
    let llvm = std::env::var("NX_LLVM").ok().map(|d| PathBuf::from(d).join("clang-cl.exe"));
    let tc_llvm = std::env::var("NX_TOOLCHAIN").ok().map(|d| PathBuf::from(d).join("llvm").join("bin").join("clang-cl.exe"));
    let items: Vec<(&str, Option<PathBuf>, &str)> = vec![
        ("Visual Studio C++ tools", find_tool(&[], &[pf86(r"Microsoft Visual Studio\Installer\vswhere.exe")]),
         "Visual Studio 2022 Build Tools or newer with \"Desktop development with C++\"."),
        ("LLVM (clang-cl)", find_tool(&["clang-cl.exe"], &[llvm.unwrap_or_default(), tc_llvm.unwrap_or_default(), pf(r"LLVM\bin\clang-cl.exe")]),
         "LLVM 23 or newer from github.com/llvm/llvm-project/releases (or set NX_LLVM to its bin folder, or NX_TOOLCHAIN to a folder with llvm\\bin)."),
        ("CMake", find_tool(&["cmake.exe"], &[pf(r"CMake\bin\cmake.exe")]), "CMake 3.25 or newer (winget install Kitware.CMake)."),
        ("Ninja", find_tool(&["ninja.exe"], &[local_appdata(r"Microsoft\WinGet\Links\ninja.exe")]), "winget install Ninja-build.Ninja"),
        ("uv (Python)", find_tool(&["uv.exe"], &[local_appdata(r"Microsoft\WinGet\Links\uv.exe")]), "winget install astral-sh.uv"),
        (".NET SDK", find_tool(&["dotnet.exe"], &[pf(r"dotnet\dotnet.exe")]), ".NET 10 SDK (winget install Microsoft.DotNet.SDK.10)."),
        ("Git", find_tool(&["git.exe"], &[pf(r"Git\cmd\git.exe")]), "winget install Git.Git (fetches the shader translator's sources)."),
    ];
    items
        .into_iter()
        .map(|(name, p, hint)| {
            // a clang-cl older than 22 is listed as missing: the Visual Studio 18 C++ library (MSVC 14.52) refuses it with STL1000
            let old = name.starts_with("LLVM") && p.as_deref().and_then(clang_major).is_some_and(|v| v < 22);
            let found = p.as_ref().map(|p| crate::dump::fwd(p)).unwrap_or_default();
            Tool { name: name.into(), ok: p.is_some() && !old, found, hint: hint.into() }
        })
        .collect()
}

/// The major version clang-cl reports ("clang version 23.1.3" gives 23); None when it does not run or says something else.
fn clang_major(exe: &Path) -> Option<u32> {
    let mut cmd = Command::new(exe);
    cmd.arg("--version").stdin(Stdio::null()).stderr(Stdio::null());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let out = cmd.output().ok()?;
    let text = String::from_utf8_lossy(&out.stdout);
    let rest = &text[text.find("clang version ")? + "clang version ".len()..];
    rest.split(|c: char| !c.is_ascii_digit()).next()?.parse().ok()
}

/// The Ryujinx source tree the shader translator is built from (MIT). `RYUBING_DIR`, else `<repo>/work/refs/ryubing`, cloned on
/// first use from the project's public repository.
const RYUBING_GIT: &str = "https://git.ryujinx.app/ryubing/ryujinx.git";
/// The revision tools/shaders' patches were made against (tools/shaders/scripts/setup_ryujinx.ps1).
const RYUBING_COMMIT: &str = "a82350bb774f70fcbd41c9987bf67a3775409963";

fn run_package_steps(r: &Runner, p: &Paths, tools: &Path, jobs: usize) -> Result<bool, String> {
    let game = &p.game_dir;
    fs::create_dir_all(game).map_err(|e| format!("cannot create {}: {}", game.display(), e))?;
    if !p.python().is_file() {
        return Err(format!("The bundled Python is missing ({}). Extract the release zip again so tools/ is complete.", crate::dump::fwd(&p.python())));
    }
    if !package_build_ok(p) {
        let mut c = Command::new(p.python());
        c.arg("-I").arg(tools.join("build").join("fefw_build.py")).arg("--exefs").arg(&p.exefs).arg("--game").arg(game);
        c.args(["--jobs", &jobs.to_string()]);
        let variant = crate::cpu::build_variant();
        if let Some(v) = variant {
            c.args(["--cpu", v]);
        }
        let what = if variant == Some("compat") {
            "Recompiling the game code from your dump (the compatible build for processors without FMA3)"
        } else {
            "Recompiling the game code from your dump"
        };
        if !r.run("build", what, c, game)? {
            return Ok(false);
        }
        *BUILD_CHECK.lock().unwrap() = None;
    }
    shader_steps(r, p, jobs, game)?;
    if r.shared.cancel.load(Ordering::SeqCst) {
        return Ok(false);
    }
    r.progress("ini", 0, 0, "Writing ObeliskChamber.ini");
    crate::settings::ensure_play_paths(&p.game_dir, p)?;
    Ok(true)
}

/// Size and modification time of a file ("" when missing): enough to tell that it was replaced.
fn file_stamp(path: &Path) -> String {
    fs::metadata(path)
        .map(|m| {
            let t = m.modified().ok().and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0);
            format!("{}:{}", m.len(), t)
        })
        .unwrap_or_default()
}

/// What the shader corpus was extracted from; a corpus whose marker differs is extracted again.
fn corpus_marker(p: &Paths) -> serde_json::Value {
    serde_json::json!({
        "translator_version": SHADER_TRANSLATOR_VERSION,
        "romfs": crate::dump::fwd(&p.romfs),
        "exefs": crate::dump::fwd(&p.plain_exefs()),
        "main": file_stamp(&p.plain_exefs().join("main")),
        "extractor": file_stamp(&p.extractor()),
    })
}

/// Blocks the harness set aside because they crashed the translator (status "crash" in results.tsv).
fn crashed_shaders(p: &Paths) -> usize {
    fs::read_to_string(p.spirv().join("results.tsv"))
        .unwrap_or_default()
        .lines()
        .skip(1)
        .filter(|l| l.split('\t').nth(4) == Some("crash"))
        .count()
}

/// Finds the shaders in the romfs and main and translates them to SPIR-V (both modes).
fn shader_steps(r: &Runner, p: &Paths, jobs: usize, cwd: &Path) -> Result<bool, String> {
    if !shaders_ok(p) && !translate_shaders(r, p, jobs, cwd)? {
        return Ok(false);
    }
    lighting_step(r, p, cwd)
}

fn translate_shaders(r: &Runner, p: &Paths, jobs: usize, cwd: &Path) -> Result<bool, String> {
    if !p.harness().is_file() {
        return Err(format!("The shader translator is missing ({}).", crate::dump::fwd(&p.harness())));
    }
    let _ = fs::remove_file(p.shader_stamp());
    // The corpus is deleted once the shaders are translated, so one that is still there is from a Set up that stopped in
    // the translation. Its marker (written only after the extractor finished) says which dump and extractor made it.
    let marker = p.corpus().join("setup-corpus.json");
    let want = corpus_marker(p);
    let have: serde_json::Value = serde_json::from_str(&fs::read_to_string(&marker).unwrap_or_default()).unwrap_or_default();
    if have == want && p.corpus().join("unique.tsv").is_file() {
        r.log(vec![format!("Reusing the shaders found earlier in {}", crate::dump::fwd(&p.corpus()))]);
    } else {
        let _ = fs::remove_file(&marker);
        let mut c = Command::new(p.python());
        c.arg("-I").arg(p.extractor()).arg("--romfs").arg(&p.romfs).arg("--main").arg(p.plain_exefs().join("main"));
        c.arg("--exefs").arg(p.plain_exefs()).arg("--out").arg(p.corpus()).args(["--jobs", &jobs.min(8).to_string(), "--progress"]);
        if !r.run("shaders", "Finding the shaders in your romfs", c, cwd)? {
            return Ok(false);
        }
        fs::write(&marker, want.to_string()).map_err(|e| e.to_string())?;
    }
    let intel = intel_shaders(p);
    if intel {
        r.log(vec!["[setup] intel_shaders is on: the shaders are translated with 64-bit floats lowered to 32-bit and no barriers in divergent branches (for Intel GPUs).".to_string()]);
    }
    let harness = |jobs: usize| {
        let mut c = if p.package.is_some() {
            Command::new(p.harness())
        } else {
            let mut c = Command::new("dotnet");
            c.arg(p.harness());
            c
        };
        c.arg("--batch").arg(p.corpus()).arg("--out").arg(p.spirv()).args(["--jobs", &jobs.to_string()]);
        if intel { c.args(["--no-float64", "--no-divergent-barriers"]); }
        // SPIR-V validation needs spirv-val from the Vulkan SDK, which players do not have (the release package never validates)
        if p.package.is_some() || (std::env::var_os("VULKAN_SDK").is_none() && which("spirv-val.exe").is_none()) {
            c.arg("--no-val");
        }
        c
    };
    // The harness survives translator crashes itself (it runs the translation in a child process and sets a crashing
    // shader aside, GitHub issue #46). If it still fails, once more with one job: it resumes from its journal.tsv.
    match r.run("shaders", "Translating the shaders to SPIR-V", harness(jobs), cwd) {
        Ok(true) => {}
        Ok(false) => return Ok(false),
        Err(e) => {
            r.log(vec![format!("The shader translation failed ({}); trying again with one job, from where it stopped.", e.lines().next().unwrap_or(""))]);
            if !r.run("shaders", "Translating the shaders to SPIR-V (again, one at a time)", harness(1), cwd)? {
                return Ok(false);
            }
        }
    }
    let crashed = crashed_shaders(p);
    if crashed > 0 {
        r.log(vec![format!("{crashed} shaders crashed the translator and were left out; the game draws without them (status \"crash\" in {}).",
                           crate::dump::fwd(&p.spirv().join("results.tsv")))]);
    }
    let stamp = serde_json::json!({ "complete": true, "translator_version": SHADER_TRANSLATOR_VERSION, "romfs": crate::dump::fwd(&p.romfs), "crashed": crashed, "intel_shaders": intel });
    fs::write(p.shader_stamp(), stamp.to_string()).map_err(|e| e.to_string())?;
    // the corpus is only the extractor's output for the translator: several GB the game never reads
    let _ = fs::remove_dir_all(p.corpus());
    Ok(true)
}

fn run_steps(r: &Runner, p: &Paths, jobs: usize) -> Result<bool, String> {
    if let Some(tools) = &p.package {
        return run_package_steps(r, p, tools, jobs);
    }
    let repo = &p.repo;
    let py = p.python();
    if !python_ok(p) {
        let uv = find_tool(&["uv.exe"], &[local_appdata(r"Microsoft\WinGet\Links\uv.exe")]).ok_or("uv was not found. Install it (winget install astral-sh.uv) and press Set up again.")?;
        if !py.is_file() {
            let mut c = Command::new(&uv);
            c.args(["venv", "tools/.venv", "--python", "3.13"]);
            if !r.run("python", "Creating the Python environment (tools/.venv)", c, repo)? {
                return Ok(false);
            }
        }
        let mut c = Command::new(&uv);
        c.args(["pip", "install", "--python", "tools/.venv", "-r", "recomp/requirements.txt"]);
        if !r.run("python", "Installing the Python packages", c, repo)? {
            return Ok(false);
        }
    }
    if !exefs_ok(p) {
        let mut c = Command::new(&py);
        c.arg("-I").arg("tools/nso/prepare_exefs.py").arg("--exefs").arg(&p.exefs).arg("--out").arg(p.plain_exefs());
        if !r.run("exefs", "Reading the game code from your dump", c, repo)? {
            return Ok(false);
        }
    }
    if !libs_ok(p) || mod_sites_problem(p).is_some() || !mods_missing_lift_sites(p).is_empty() {
        let sites = lift_sites_file(p)?;
        let mut c = Command::new("cmd.exe");
        c.args(["/d", "/c", "recomp\\scripts\\build_game.cmd", "fefw"]).env("NX_JOBS", jobs.to_string()).env("FEFW_SITES", &sites);
        if !r.run("lift", "Recompiling the game code (an hour or more the first time)", c, repo)? {
            return Ok(false);
        }
    }
    if !textures_ok(p) {
        let mut c = Command::new(powershell());
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "tools/textures/scripts/fetch_third_party.ps1"]);
        if !r.run("textures", "Fetching the texture transcoder sources (astc-encoder, bc7enc)", c, repo)? {
            return Ok(false);
        }
    }
    if !exe_ok(p) {
        let mut c = Command::new(powershell());
        c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "game-build\\scripts\\build_fefw.ps1", "-Jobs", &jobs.to_string(), "-LibDir"])
            .arg(p.libs());   // an OsStr argument: any folder name works
        if !r.run("exe", "Building ObeliskChamber.exe", c, repo)? {
            return Ok(false);
        }
    }
    if !p.harness().is_file() || !shaders_ok(p) {
        let staged = repo.join("tools").join("shaders").join("third_party").join("ryubing");
        if !staged.is_dir() {
            let src = std::env::var("RYUBING_DIR").map(PathBuf::from).unwrap_or_else(|_| repo.join("work").join("refs").join("ryubing"));
            if !src.is_dir() {
                let git = find_tool(&["git.exe"], &[]).ok_or("Git was not found; it is needed once to fetch the shader translator's sources.")?;
                let mut c = Command::new(&git);
                c.args(["clone", "--filter=blob:none", "--no-checkout", RYUBING_GIT]).arg(&src);
                if !r.run("harness", "Fetching the Ryujinx shader translator sources (MIT)", c, repo)? {
                    return Ok(false);
                }
                let mut c = Command::new(&git);
                c.arg("-C").arg(&src).args(["checkout", RYUBING_COMMIT]);
                if !r.run("harness", "Checking out the translator's tested revision", c, repo)? {
                    return Ok(false);
                }
            }
            let mut c = Command::new(powershell());
            c.args(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "tools\\shaders\\scripts\\setup_ryujinx.ps1", "-Ryubing"]).arg(&src);
            if !r.run("harness", "Staging the translator sources", c, repo)? {
                return Ok(false);
            }
        }
        let mut c = Command::new("dotnet");
        c.args(["build", "tools\\shaders\\harness\\SpirvHarness.csproj", "-c", "Release"]);
        if !r.run("harness", "Building the shader translator", c, repo)? {
            return Ok(false);
        }
    }
    if !shader_steps(r, p, jobs, repo)? {
        return Ok(false);
    }
    r.progress("ini", 0, 0, "Writing ObeliskChamber.ini");
    crate::settings::ensure_play_paths(&p.game_dir, p)?;
    r.log(vec![format!("ObeliskChamber.ini is in {}", crate::dump::fwd(&p.game_dir))]);
    Ok(true)
}

/// Runs the whole setup on this thread. Returns the final progress (done, cancelled or error).
pub fn run(shared: &Arc<Shared>, p: &Paths, jobs: usize, emit: &(dyn Fn(Progress) + Sync), emit_log: &(dyn Fn(Vec<String>) + Sync)) -> Progress {
    let _ = fs::create_dir_all(p.repo.join("work"));
    let file = fs::OpenOptions::new().create(true).append(true).open(p.log()).ok();
    let r = Runner { shared, emit, emit_log, log_file: Mutex::new(file), log_path: p.log() };
    r.log(vec![format!("--- setup: dump exefs {}, romfs {}", crate::dump::fwd(&p.exefs), crate::dump::fwd(&p.romfs))]);
    let end = match run_steps(&r, p, jobs) {
        Ok(true) => Progress { state: "done".into(), message: "Setup finished. Press Play.".into(), ..Default::default() },
        Ok(false) => Progress { state: "cancelled".into(), message: "Paused. Set up continues where it stopped.".into(), ..Default::default() },
        Err(e) => {
            r.log(vec![format!("error: {e}")]);
            Progress { state: "error".into(), message: e, ..Default::default() }
        }
    };
    *shared.progress.lock().unwrap() = end.clone();
    end
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn progress_lines() {
        assert_eq!(ninja_progress("[12/345] Building CXX object x.obj"), Some((12, 345)));
        assert_eq!(ninja_progress("[ 3/ 9] x"), Some((3, 9)));
        assert_eq!(ninja_progress("no"), None);
        assert_eq!(tool_progress("scan: 5 10"), Some((5, 10)));
    }

    #[test]
    fn mod_build_ids() {
        let main = "DB93B0125F4850601D61C3EDDE012F8EB2ECFE77000000000000000000000000";
        assert!(same_build("DB93B0125F4850601D61C3EDDE012F8EB2ECFE77", main));
        assert!(same_build("db93b0125f4850601d61c3edde012f8eb2ecfe77", main));
        assert!(!same_build("DB93B0125F485060", main)); // the digits after it are not all zeros
        assert!(!same_build("DB93B012", main)); // too short to name a build
        assert!(!same_build("45EAC7636FFC32C7", main));
        assert!(same_build("45EAC7636FFC32C7", "")); // no build ID known: assume it
    }

    /// A run folder with one code mod: its sites are reported until the exe's site list has them, and the lift's list adds them.
    #[test]
    fn mod_sites_against_the_build() {
        let root = std::env::temp_dir().join(format!("fefw-sites-test-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        let repo = root.join("repo");
        let game = root.join("play");
        let p = Paths {
            repo: repo.clone(),
            exefs: root.join("dump"),
            romfs: root.join("dump"),
            dlc: None,
            game_dir: game.clone(),
            package: None,
            lighting_edit: false,
        };
        // main's NSO header with its build ID at +0x40
        let mut nso = vec![0u8; 0x60];
        nso[0x18..0x1C].copy_from_slice(&0x0200_0000u32.to_le_bytes()); // .text size: main's code covers every site below
        nso[0x40..0x54].copy_from_slice(&[0xDB, 0x93, 0xB0, 0x12, 0x5F, 0x48, 0x50, 0x60, 0x1D, 0x61, 0xC3, 0xED, 0xDE, 0x01, 0x2F, 0x8E, 0xB2, 0xEC, 0xFE, 0x77]);
        fs::create_dir_all(p.plain_exefs()).unwrap();
        fs::write(p.plain_exefs().join("main"), &nso).unwrap();
        let own = repo.join("recomp").join("games").join("fefw").join("main.sites");
        fs::create_dir_all(own.parent().unwrap()).unwrap();
        fs::write(&own, "# hook sites of main\ninline 0x71007bf1c0\nmidhook 0x710188bd30\n").unwrap();
        let ex = game.join("mods").join("Some_Mod").join("exefs");
        fs::create_dir_all(&ex).unwrap();
        fs::write(
            ex.join("some.sites"),
            "@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x710188bd30   # in main.sites already\nmidhook 0x71016f21f4\n",
        )
        .unwrap();
        // a mod for another game build is left out
        let other = game.join("mods").join("Old_Mod").join("exefs");
        fs::create_dir_all(&other).unwrap();
        fs::write(other.join("old.sites"), "@nsobid 45EAC7636FFC32C7\nmidhook 0x7100001000\n").unwrap();

        assert_eq!(mod_sites(&p).unwrap().len(), 2); // the two midhooks of Some_Mod
        // no exe list yet: nothing to compare with
        assert!(mods_missing_sites(&p).is_empty());
        // no lift list yet: compared with base main.sites, missing Some_Mod's new site
        assert_eq!(mods_missing_lift_sites(&p), vec!["Some_Mod".to_string()]);
        let list = p.exe().with_extension("sites");
        fs::create_dir_all(list.parent().unwrap()).unwrap();
        fs::write(&list, fs::read_to_string(&own).unwrap()).unwrap();
        assert_eq!(mods_missing_sites(&p), vec!["Some_Mod".to_string()]);

        let lift = lift_sites_file(&p).unwrap();
        let text = fs::read_to_string(&lift).unwrap();
        assert!(text.contains("midhook 0x71016f21f4   # Some_Mod/exefs/some.sites:3"));
        assert_eq!(text.matches("0x710188bd30").count(), 1); // not added twice
        assert!(!text.contains("0x7100001000"));
        // libraries lifted from that list: nothing missing in lift step
        fs::create_dir_all(p.libs()).unwrap();
        fs::write(p.libs().join("main.sites"), &text).unwrap();
        assert!(mods_missing_lift_sites(&p).is_empty());
        // the exe built from that list: nothing missing any more
        fs::write(&list, &text).unwrap();
        assert!(mods_missing_sites(&p).is_empty());

        // problems are reported with the release script's messages, and the lift step is not done meanwhile
        let bad = ex.join("bad.sites");
        let check = |text: &str, want: &str| {
            fs::write(&bad, text).unwrap();
            let e = mod_sites(&p).unwrap_err();
            assert!(e.contains(want), "{e}");
            assert_eq!(missing_sites_detail(&p), e);
        };
        // a kind a mod cannot add here (another port's, or a later one) is skipped, not an error (fefw-recomp issue #165)
        fs::write(&bad, "@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\npatch 0x7100000000\ndynimm 0x7100000004\n").unwrap();
        assert!(mod_sites(&p).is_ok());
        check("@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook zz\n", "bad.sites:2: expected \"<kind> <guest address in hex>\"");
        check("midhook 0x71016f21f4\n", "Some_Mod/exefs/bad.sites has no @nsobid line");
        check("@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x71016f21f2\n", "is not an instruction address in main");
        // main's code: .text at memory offset 0 with 0x1000 bytes (NSO header +0x14 / +0x18)
        nso[0x18..0x1C].copy_from_slice(&0x1000u32.to_le_bytes());
        fs::write(p.plain_exefs().join("main"), &nso).unwrap();
        check("@nsobid DB93B0125F4850601D61C3EDDE012F8EB2ECFE77\nmidhook 0x7100002000\n", "0x7100002000 is outside main's code");
        let _ = fs::remove_dir_all(&root);
    }
}
