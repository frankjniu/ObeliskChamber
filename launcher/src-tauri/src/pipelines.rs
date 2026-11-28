// SPDX-License-Identifier: GPL-3.0-or-later
//! The pipeline build before play (S1). `ObeliskChamber.exe --build-pipelines` builds every pipeline the player's own earlier sessions
//! recorded (pipeline_keys.bin in the run folder) into the pipeline cache on most of the cores, with no game running, so the boot prewarm
//! and the first visit to each place do not compete with the game for them. Nothing is shipped: a player with no recorded keys has
//! nothing to build.
//!
//! The launcher runs it from the Play page's "Build pipelines" button, and before Play when the Settings box "Build pipelines before
//! play" is on and one of these says it is due (reasons()): no build yet, a Set up since the last build (ObeliskChamber.exe is newer than
//! the status file), a graphics driver change (Windows: the display drivers' DriverVersion in the registry differs from the one of the last
//! build), or the last session's log saying the driver's cache was cold. The exe itself decides what is left to build (its
//! pipeline_keys.bin.built list per GPU and driver build): a build with nothing new ends after the device start.
//!
//! The exe writes pipeline_build.txt (`key: value` lines, game-build/fefw/src/fefw_build_pipelines.h), read here every 200 ms; its log
//! goes to pipeline_build.log, so ObeliskChamber.log keeps the last play session. Skip creates pipeline_build.txt.stop: the exe saves what
//! it built and ends; after SKIP_GRACE it is ended by force (what it saved before stays).

use crate::play::{self, Status};
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant, SystemTime};
use tauri::AppHandle;

pub const STATUS_FILE: &str = "pipeline_build.txt";
pub const LOG_FILE: &str = "pipeline_build.log";
const GAME_LOG: &str = "ObeliskChamber.log";
const SKIP_GRACE: Duration = Duration::from_secs(20);

static SKIP: AtomicBool = AtomicBool::new(false);

/// The exe's status file.
#[derive(Default, Clone, Debug, PartialEq)]
pub struct BuildStatus {
    /// starting, running, ready, done, stopped, error ("" when the file has none)
    pub state: String,
    pub done: u64,
    pub total: u64,
    pub keys: u64,
    pub built: u64,
    pub failed: u64,
    pub threads: u64,
    pub seconds: f64,
    pub median_ms: f64,
    pub gpu: String,
    pub identity: String,
    pub message: String,
}

pub fn parse_status(text: &str) -> BuildStatus {
    let mut s = BuildStatus::default();
    for line in text.lines() {
        let Some((k, v)) = line.split_once(':') else { continue };
        let v = v.trim();
        let n = || v.parse::<u64>().unwrap_or(0);
        let f = || v.parse::<f64>().unwrap_or(0.0);
        match k.trim() {
            "state" => s.state = v.to_string(),
            "done" => s.done = n(),
            "total" => s.total = n(),
            "keys" => s.keys = n(),
            "built" => s.built = n(),
            "failed" => s.failed = n(),
            "threads" => s.threads = n(),
            "seconds" => s.seconds = f(),
            "median_ms" => s.median_ms = f(),
            "gpu" => s.gpu = v.to_string(),
            "identity" => s.identity = v.to_string(),
            "message" => s.message = v.to_string(),
            _ => {}
        }
    }
    s
}

fn read_status(dir: &Path) -> Option<BuildStatus> {
    std::fs::read_to_string(dir.join(STATUS_FILE)).ok().map(|t| parse_status(&t))
}

fn mtime(p: &Path) -> Option<SystemTime> {
    std::fs::metadata(p).and_then(|m| m.modified()).ok()
}

/// The key file the game records into: [nvn] pipeline_keys of ObeliskChamber.ini, else pipeline_keys.bin, in the run folder unless the
/// ini gives an absolute path (Z:/x is /x on Linux).
fn keys_file(dir: &Path) -> PathBuf {
    let ini = crate::ini::Ini::parse(&std::fs::read_to_string(crate::settings::game_ini(dir)).unwrap_or_default());
    let v = ini.get("nvn", "pipeline_keys").unwrap_or_default().replace('\\', "/");
    let v = if v.trim().is_empty() { "pipeline_keys.bin".to_string() } else { v.trim().to_string() };
    if !cfg!(windows) {
        if let Some(rest) = v.strip_prefix("Z:").or_else(|| v.strip_prefix("z:")) {
            return PathBuf::from(rest);
        }
    }
    let p = PathBuf::from(&v);
    if p.is_absolute() {
        p
    } else {
        dir.join(p)
    }
}

/// The key file holds at least one record (16 bytes of header, then the records).
fn has_keys(dir: &Path) -> bool {
    std::fs::metadata(keys_file(dir)).map(|m| m.len() > 16).unwrap_or(false)
}

/// The runtime's lines that say a session ran with a cold driver cache: the FR4 line of the boot prewarm (nvn_prewarm.cpp) and the pipeline
/// cache file of another driver build or GPU (nvn_pipeline.cpp).
pub fn cold_in_log(text: &str) -> bool {
    text.lines().any(|l| {
        l.contains("the driver's cache is cold") || (l.contains("pipeline cache") && (l.contains("made by another driver build") || l.contains("made on another GPU")))
    })
}

/// The start of a log (the lines above come in the first minutes of a session).
fn log_head(p: &Path) -> String {
    let mut buf = Vec::new();
    if let Ok(f) = std::fs::File::open(p) {
        let _ = f.take(8 << 20).read_to_end(&mut buf);
    }
    String::from_utf8_lossy(&buf).into_owned()
}

/// The display adapters' driver versions from `reg query <display class> /s /v DriverVersion`, sorted and joined by commas. None when
/// the output has none.
pub fn parse_driver_versions(reg_output: &str) -> Option<String> {
    let mut v: Vec<String> = reg_output
        .lines()
        .filter_map(|l| {
            let mut w = l.split_whitespace();
            (w.next()? == "DriverVersion" && w.next()? == "REG_SZ").then(|| w.collect::<Vec<_>>().join(" "))
        })
        .filter(|s| !s.is_empty())
        .collect();
    v.sort();
    v.dedup();
    (!v.is_empty()).then(|| v.join(","))
}

/// What identifies the graphics drivers on this PC now (Windows: the display class's DriverVersion values). None elsewhere (Wine has
/// no real one: there the cold-cache line of the log and the exe's own check do the work) or when the registry cannot be read.
pub fn driver_fingerprint() -> Option<String> {
    if !cfg!(windows) {
        return None;
    }
    let mut cmd = Command::new("reg");
    cmd.args(["query", r"HKLM\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}", "/s", "/v", "DriverVersion"]);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        cmd.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    let out = cmd.stdin(Stdio::null()).stderr(Stdio::null()).output().ok()?;
    parse_driver_versions(&String::from_utf8_lossy(&out.stdout))
}

/// When the run folder's ObeliskChamber.log was last written (seconds since 1970; 0: none): the launcher keeps the one a build was started
/// for, so one cold session starts one build.
pub fn log_stamp(dir: &Path) -> u64 {
    mtime(&dir.join(GAME_LOG)).and_then(|t| t.duration_since(SystemTime::UNIX_EPOCH).ok()).map_or(0, |d| d.as_secs())
}

/// Why a build is due before play (empty: it is not). `last_driver`: the fingerprint stored after the last build that ended (done, ready
/// or error); `driver_now`: driver_fingerprint(); `cold_log_seen`: the log_stamp a build was last started for.
pub fn reasons(dir: &Path, exe: &Path, last_driver: &str, driver_now: Option<&str>, cold_log_seen: u64) -> Vec<String> {
    let mut v = Vec::new();
    if !has_keys(dir) {
        return v; // nothing recorded yet: play records, the next start builds
    }
    let (Some(last), Some(built_at)) = (read_status(dir), mtime(&dir.join(STATUS_FILE))) else {
        v.push("no pipeline build yet".to_string());
        return v;
    };
    // a build the player skipped goes on at the next Play (it keeps what it built and builds the rest)
    if last.state == "stopped" || last.state == "running" || last.state == "starting" {
        v.push("the last build did not finish".to_string());
    }
    if mtime(exe).is_some_and(|t| t > built_at) {
        v.push("Set up ran since the last build".to_string());
    }
    if let Some(now) = driver_now {
        if now != last_driver {
            v.push("the graphics driver changed".to_string());
        }
    }
    // A cold session after the last build, once per log. Not after a build that found nothing to do: that build cannot warm what the
    // session found cold (the exe's own check is the authority), and asking again would start a build at every Play.
    let log = dir.join(GAME_LOG);
    if last.state != "ready" && log_stamp(dir) != cold_log_seen && mtime(&log).is_some_and(|t| t > built_at) && cold_in_log(&log_head(&log)) {
        v.push("the last session found the driver's cache cold".to_string());
    }
    v
}

/// The Skip button: the running build saves what it has and ends.
pub fn skip() {
    SKIP.store(true, Ordering::SeqCst);
}

fn building(app: &AppHandle, message: &str, done: u64, total: u64) {
    play::set_status(app, Status { state: "building".into(), message: message.into(), done, total, ..Default::default() });
}

/// The line the Play page shows for the end of a build (rusttext.js translates these).
fn outcome(st: &BuildStatus, skipped: bool, code: Option<i32>) -> String {
    match st.state.as_str() {
        "done" if st.failed > 0 => format!("Pipelines built: {} in {:.0} s ({} could not be built).", st.built, st.seconds, st.failed),
        "done" => format!("Pipelines built: {} in {:.0} s.", st.built, st.seconds),
        "ready" => "Pipelines are already built for this GPU and driver.".to_string(),
        "stopped" => "The pipeline build stopped; what was built is kept.".to_string(),
        "error" if st.message.starts_with("ended without a result") => {
            format!("The pipeline build ended without a result (exit code {}). See {LOG_FILE}.", code.map_or("none".to_string(), |c| c.to_string()))
        }
        "error" => format!("The pipeline build failed: {}", st.message),
        _ if skipped => "The pipeline build stopped; what was built is kept.".to_string(),
        _ => format!("The pipeline build ended without a result (exit code {}). See {LOG_FILE}.", code.map_or("none".to_string(), |c| c.to_string())),
    }
}

/// Starts the build. `play_after`: Some(minimize) starts the game when it ends, whatever the outcome (a build that failed or was skipped
/// leaves the game to build what it needs, as before). `remember` runs when it has ended, with its final status (state done, ready,
/// stopped or error).
#[allow(clippy::too_many_arguments)]
pub fn start(
    app: &AppHandle,
    exe: &Path,
    game_dir: &Path,
    launcher_dir: &Path,
    runner_setting: &str,
    force: bool,
    play_after: Option<bool>,
    remember: Box<dyn FnOnce(&BuildStatus) + Send>,
) -> Result<(), String> {
    if play::busy(app) {
        return Err("The game is already running.".into());
    }
    if !exe.is_file() {
        return Err(format!("ObeliskChamber.exe was not found at {}. Run Set up first.", crate::dump::fwd(exe)));
    }
    let runner = play::runner(runner_setting)?;
    let (app, exe, dir, launcher_dir, runner_setting) = (app.clone(), exe.to_path_buf(), game_dir.to_path_buf(), launcher_dir.to_path_buf(), runner_setting.to_string());
    SKIP.store(false, Ordering::SeqCst);
    building(&app, "Building pipelines...", 0, 0);
    std::thread::spawn(move || {
        let status_path = dir.join(STATUS_FILE);
        let stop_path = dir.join(format!("{STATUS_FILE}.stop"));
        let _ = std::fs::remove_file(&status_path);
        let _ = std::fs::remove_file(&stop_path);
        let mut cmd = play::game_command(&exe, &dir, &launcher_dir, &runner);
        cmd.args(["--build-pipelines", "--status", STATUS_FILE, "--log", LOG_FILE]);
        cmd.env("NXRT_LOG", LOG_FILE); // ObeliskChamber.ini's [log] path would win over --log: the environment wins over it
        if force {
            cmd.arg("--force");
        }
        cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        #[cfg(unix)]
        {
            use std::os::unix::process::CommandExt;
            cmd.process_group(0); // kill_tree ends Proton's or Wine's processes with it
        }
        let (st, skipped, code) = match cmd.spawn() {
            Err(e) => (BuildStatus { state: "error".into(), message: format!("could not start ObeliskChamber.exe: {e}"), ..Default::default() }, false, None),
            Ok(mut child) => {
                let mut skip_at: Option<Instant> = None;
                let mut shown = (0u64, 0u64);
                let code = loop {
                    match child.try_wait() {
                        Ok(Some(s)) => break s.code(),
                        Ok(None) => {}
                        Err(_) => break None,
                    }
                    if SKIP.load(Ordering::SeqCst) && skip_at.is_none() {
                        let _ = std::fs::write(&stop_path, b"stop\n");
                        skip_at = Some(Instant::now());
                        building(&app, "Stopping the pipeline build...", shown.0, shown.1);
                    }
                    if skip_at.is_some_and(|t| t.elapsed() >= SKIP_GRACE) {
                        crate::setup::kill_tree(child.id());
                        let _ = child.kill();
                        break child.wait().ok().and_then(|s| s.code());
                    }
                    if skip_at.is_none() {
                        if let Some(s) = read_status(&dir) {
                            if s.state == "running" && s.total > 0 && (s.done, s.total) != shown {
                                shown = (s.done, s.total);
                                building(&app, &format!("Building pipelines: {} of {}", s.done, s.total), s.done, s.total);
                            }
                        }
                    }
                    std::thread::sleep(Duration::from_millis(200));
                };
                (read_status(&dir).unwrap_or_default(), skip_at.is_some(), code)
            }
        };
        let _ = std::fs::remove_file(&stop_path);
        let mut st = st;
        if st.state.is_empty() || st.state == "starting" || st.state == "running" {
            // ended without a result of its own (it crashed, or Skip had to end it): the file says so, so that the next Play does not
            // start it again unless something changes (a skip is resumed: reasons())
            st.state = if skipped { "stopped".into() } else { "error".into() };
            if st.message.is_empty() {
                st.message = format!("ended without a result (exit code {})", code.map_or("none".to_string(), |c| c.to_string()));
            }
            let _ = std::fs::write(&status_path, format!("state: {}\ndone: {}\ntotal: {}\nmessage: {}\n", st.state, st.done, st.total, st.message));
        }
        remember(&st);
        let message = outcome(&st, skipped, code);
        play::set_status(&app, Status { state: "idle".into(), message: message.clone(), ..Default::default() });
        if let Some(minimize) = play_after {
            if let Err(e) = play::start(&app, &exe, &dir, &launcher_dir, minimize, &runner_setting) {
                play::set_status(&app, Status { state: "exited".into(), crashed: true, message: e, ..Default::default() });
            }
        }
    });
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_status_file_is_read() {
        let t = "state: done\ndone: 4744\ntotal: 4744\nkeys: 4800\nbuilt: 4740\nfailed: 4\nthreads: 14\nseconds: 41.2\nmedian_ms: 97.50\n\
                 gpu: NVIDIA GeForce RTX 3070 Laptop GPU\nidentity: 10de:24dd:00ff driver=1 salt=\nmessage: 4 pipelines could not be built: see the log\n";
        let s = parse_status(t);
        assert_eq!(s.state, "done");
        assert_eq!((s.done, s.total, s.keys, s.built, s.failed, s.threads), (4744, 4744, 4800, 4740, 4, 14));
        assert!((s.seconds - 41.2).abs() < 1e-9 && (s.median_ms - 97.5).abs() < 1e-9);
        assert_eq!(s.gpu, "NVIDIA GeForce RTX 3070 Laptop GPU");
        assert_eq!(s.identity, "10de:24dd:00ff driver=1 salt=");
        assert_eq!(s.message, "4 pipelines could not be built: see the log");
        assert_eq!(parse_status("garbage\nstate:\ndone: x\n"), BuildStatus::default());
        assert_eq!(outcome(&s, false, Some(0)), "Pipelines built: 4740 in 41 s (4 could not be built).");
        assert_eq!(outcome(&BuildStatus::default(), true, None), "The pipeline build stopped; what was built is kept.");
    }

    #[test]
    fn a_cold_session_is_found_in_its_log() {
        assert!(cold_in_log("[I] nvn.prewarm: prewarm: the first 64 builds took 9.1 ms each (median): the driver's cache is cold (a new driver, a new GPU or a cleared cache)\n"));
        assert!(cold_in_log("[W] nvn.pipe: pipeline cache pipeline_cache.bin (123 bytes) is out of date: not used, pipelines build again from scratch this time (made by another driver build (a driver update))\n"));
        assert!(!cold_in_log("[I] nvn.prewarm: prewarm: the first 64 builds took 0.8 ms each (median): the driver's cache has them (warm)\n"));
    }

    #[test]
    fn driver_versions_come_from_reg_output() {
        let out = "\r\nHKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0000\r\n    DriverVersion    REG_SZ    32.0.15.6094\r\n\r\n\
                   HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0001\r\n    DriverVersion    REG_SZ    31.0.101.5590\r\n\r\nEnd of search: 2 match(es) found.\r\n";
        assert_eq!(parse_driver_versions(out).as_deref(), Some("31.0.101.5590,32.0.15.6094"));
        assert_eq!(parse_driver_versions("ERROR: The system was unable to find the specified registry key or value.\r\n"), None);
    }

    #[test]
    fn a_build_is_due_for_the_right_reasons() {
        let dir = std::env::temp_dir().join(format!("fefw_pipelines_test_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let exe = dir.join("ObeliskChamber.exe");
        std::fs::write(&exe, b"exe").unwrap();
        // no key file: nothing to build
        assert!(reasons(&dir, &exe, "", Some("1"), 0).is_empty());
        std::fs::write(dir.join("pipeline_keys.bin"), vec![0u8; 16 + 200]).unwrap();
        assert_eq!(reasons(&dir, &exe, "", None, 0), ["no pipeline build yet"]);
        std::thread::sleep(Duration::from_millis(20));
        std::fs::write(dir.join(STATUS_FILE), "state: done\n").unwrap();
        assert!(reasons(&dir, &exe, "1", Some("1"), 0).is_empty());
        assert_eq!(reasons(&dir, &exe, "1", Some("2"), 0), ["the graphics driver changed"]);
        // a failed build is not retried by itself; a skipped one is resumed
        std::fs::write(dir.join(STATUS_FILE), "state: error\nmessage: x\n").unwrap();
        assert!(reasons(&dir, &exe, "1", Some("1"), 0).is_empty());
        std::fs::write(dir.join(STATUS_FILE), "state: stopped\n").unwrap();
        assert_eq!(reasons(&dir, &exe, "1", Some("1"), 0), ["the last build did not finish"]);
        std::fs::write(dir.join(STATUS_FILE), "state: done\n").unwrap();
        // a session after the build that found the cache cold: once per log, and not after a build that had nothing to do
        std::thread::sleep(Duration::from_millis(20));
        std::fs::write(dir.join(GAME_LOG), "x: the driver's cache is cold (a new driver)\n").unwrap();
        assert_eq!(reasons(&dir, &exe, "1", Some("1"), 0), ["the last session found the driver's cache cold"]);
        assert!(reasons(&dir, &exe, "1", Some("1"), log_stamp(&dir)).is_empty());
        // Set up made a new exe
        std::fs::write(&exe, b"exe 2").unwrap();
        assert!(reasons(&dir, &exe, "1", Some("1"), 0).contains(&"Set up ran since the last build".to_string()));
        // [nvn] pipeline_keys elsewhere, with nothing in it
        std::fs::write(dir.join("ObeliskChamber.ini"), "[nvn]\npipeline_keys = keys/other.bin\n").unwrap();
        assert!(reasons(&dir, &exe, "1", Some("1"), 0).is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_ready_build_does_not_answer_a_cold_log() {
        let dir = std::env::temp_dir().join(format!("fefw_pipelines_ready_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let exe = dir.join("ObeliskChamber.exe");
        std::fs::write(&exe, b"exe").unwrap();
        std::fs::write(dir.join("pipeline_keys.bin"), vec![0u8; 16 + 200]).unwrap();
        std::fs::write(dir.join(STATUS_FILE), "state: ready\n").unwrap();
        std::thread::sleep(Duration::from_millis(20));
        std::fs::write(dir.join(GAME_LOG), "x: the driver's cache is cold (a new driver)\n").unwrap();
        assert!(reasons(&dir, &exe, "1", Some("1"), 0).is_empty());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
