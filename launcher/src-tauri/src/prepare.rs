// SPDX-License-Identifier: GPL-3.0-or-later
//! `ObeliskChamber-launcher.exe --prepare --game <dump folder or .nup>`: Set up without a window, for RecompLauncher
//! (recomplauncher.json at the top of the release; https://github.com/elripalda/RecompLauncher/blob/main/docs/DEVELOPERS.md).
//!
//! It does what the Setup page's Set up button does, then what Play does before it starts the game (ObeliskChamber.ini's paths), so
//! RecompLauncher can start ObeliskChamber.exe itself. It never asks anything: everything comes from the arguments. On stdout it prints
//! `progress <0..1> <text>` lines, the format RecompLauncher shows under its setup step. It exits 0 when the game is ready, and 1 with
//! the reason as the last line otherwise, since that line is what the player reads first.
//!
//! Without --prepare the launcher starts as it always has.

use super::{cpu, dump, jobs_of, nup, nup_dir, paths, settings, setup, Config};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// True when the command line asks for the headless setup.
pub fn wanted(args: &[String]) -> bool {
    args.iter().any(|a| a == "--prepare")
}

/// The value of an option written as `--name value` or `--name=value`. None when it is missing or empty.
fn value(args: &[String], name: &str) -> Option<String> {
    let joined = format!("{name}=");
    for (i, a) in args.iter().enumerate() {
        let v = if a == name { args.get(i + 1).cloned() } else { a.strip_prefix(&joined).map(str::to_string) };
        if let Some(v) = v {
            let v = v.trim().to_string();
            return (!v.is_empty()).then_some(v);
        }
    }
    None
}

/// One line on stdout. A launcher built for the Windows subsystem has no console, but the pipe RecompLauncher gives it still works.
fn say(line: &str) {
    let mut out = std::io::stdout();
    let _ = writeln!(out, "{line}");
    let _ = out.flush();
}

fn progress(fraction: f64, text: &str) {
    say(&format!("progress {:.3} {}", fraction.clamp(0.0, 1.0), text));
}

/// The reason and the failure exit code. A reason of several lines (a step's message with the tail of its output) is printed whole,
/// then its first line again: RecompLauncher shows the last lines the step printed, and the player reads the last one first.
fn fail(reason: &str) -> i32 {
    let lines: Vec<&str> = reason.lines().map(str::trim).filter(|l| !l.is_empty()).collect();
    let Some(first) = lines.first() else {
        say("Set up did not finish. See setup.log in the game's folder.");
        return 1;
    };
    if lines.len() > 1 {
        for l in &lines {
            say(l);
        }
    }
    say(first);
    1
}

/// The launcher's own options that change what Set up builds (its Settings page: build processes, the decompiled lighting shader
/// box), read from its launcher.json in the app config folder as Tauri keeps it, without Tauri. A player who uses both launchers
/// then gets the same build from either; without the file, the defaults.
fn launcher_options(cfg: &mut Config) {
    const ID: &str = "io.github.goldoire.fefw-launcher";   // tauri.conf.json "identifier"
    let base = if cfg!(windows) {
        std::env::var_os("APPDATA").map(PathBuf::from)
    } else {
        std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from).or_else(|| std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config")))
    };
    let Some(file) = base.map(|b| b.join(ID).join("launcher.json")) else { return };
    let Ok(text) = std::fs::read_to_string(file) else { return };
    let Ok(own) = serde_json::from_str::<Config>(&text) else { return };
    cfg.jobs = own.jobs;
    cfg.lighting_edit = own.lighting_edit;
}

/// Prints the last progress line again every few minutes while a step prints nothing (the lift of main runs for a long time without a
/// line): RecompLauncher stops a setup step that has been quiet for an hour.
struct Heartbeat {
    stop: Arc<AtomicBool>,
    th: Option<std::thread::JoinHandle<()>>,
}

impl Heartbeat {
    fn start(last: Arc<Mutex<(f64, String, Instant)>>) -> Heartbeat {
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let th = std::thread::spawn(move || {
            while !flag.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_secs(1));
                let mut l = last.lock().unwrap();
                if l.2.elapsed() >= Duration::from_secs(300) {
                    progress(l.0, &l.1);
                    l.2 = Instant::now();
                }
            }
        });
        Heartbeat { stop, th: Some(th) }
    }
}

impl Drop for Heartbeat {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(t) = self.th.take() {
            let _ = t.join();
        }
    }
}

fn is_nup(path: &Path) -> bool {
    path.is_file() && path.extension().is_some_and(|e| e.eq_ignore_ascii_case("nup"))
}

/// A .nup is unpacked as the Setup page's .nup button does (main.rs import_nup), into the same folder, and that folder is the dump.
/// A finished unpack of the same package is reused.
fn unpack(cfg: &Config, nup_file: &Path, share: f64) -> Result<PathBuf, String> {
    let out = nup_dir(cfg)?;
    let mut shown = -1.0f64;
    let mut on_file = |done: u64, total: u64, _file: &str| {
        if total == 0 {
            return;
        }
        let f = share * (done as f64 / total as f64).min(1.0);
        if f - shown >= 0.005 {
            shown = f;
            progress(f, "Unpacking your .nup package");
        }
    };
    nup::extract(nup_file, &out, &mut on_file, &|| false)
}

/// The headless setup. Returns the exit code.
pub fn run(args: &[String]) -> i32 {
    let Some(game) = value(args, "--game") else {
        return fail("No game files were given: --prepare needs --game <your dump folder or .nup file>.");
    };
    if let Some(why) = cpu::check() {
        return fail(&why);
    }
    let mut cfg = Config { dump_dir: game.clone(), ..Default::default() };
    launcher_options(&mut cfg);
    // a .nup takes the first tenth of the bar; setup the rest
    let mut start = 0.0f64;
    let given = PathBuf::from(&game);
    if is_nup(&given) {
        match unpack(&cfg, &given, 0.1) {
            Ok(dir) => cfg.dump_dir = dump::fwd(&dir),
            Err(e) => return fail(&e),
        }
        start = 0.1;
    }
    let p = match paths(&cfg) {
        Ok(p) => p,
        Err(e) => return fail(&e),
    };

    // A new release runs this step again: when the game in the folder is already built for it, there is nothing to do but the paths.
    if setup::complete(&p) {
        if let Err(e) = play_paths(&p) {
            return fail(&e);
        }
        progress(1.0, "Already built");
        return 0;
    }

    let shared = Arc::new(setup::Shared::default());
    *shared.progress.lock().unwrap() = setup::Progress { state: "running".into(), ..Default::default() };
    shared.cancel.store(false, Ordering::SeqCst);
    // the bar moves one share per step still to do, and within a step by its own count where it has one
    let todo = setup::steps(&p, None).iter().filter(|s| s.state != "done").count().max(1) as f64;
    // (the step being run, how many steps have started, the last fraction printed, the last text printed)
    let seen: Mutex<(String, usize, f64, String)> = Mutex::new((String::new(), 0, start, String::new()));
    // the last line printed and when, for the heartbeat
    let last = Arc::new(Mutex::new((start, "Starting".to_string(), Instant::now())));
    let _beat = Heartbeat::start(last.clone());
    let emit = |pr: setup::Progress| {
        let mut s = seen.lock().unwrap();
        if s.0 != pr.step {
            s.0 = pr.step.clone();
            s.1 += 1;
        }
        let within = if pr.total > 0 { (pr.done as f64 / pr.total as f64).min(1.0) } else { 0.0 };
        let steps_before = s.1.saturating_sub(1) as f64;
        let f = start + (1.0 - start) * ((steps_before + within) / todo).min(1.0);
        let text = pr.message.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or(&pr.step).to_string();
        // a step's own count starts again in each of its phases (the build: verify, lift, compile, link): the bar never goes back
        let f = f.max(s.2);
        // every update would be thousands of lines during the lift: a new message, or half a percent more, is enough
        if text != s.3 || f - s.2 >= 0.005 {
            s.2 = f;
            s.3 = text.clone();
            progress(f, &text);
            *last.lock().unwrap() = (f, text, Instant::now());
        }
    };
    // the setup's own log lines (also in setup.log in the game folder): RecompLauncher keeps all the step printed for its Open Full Log
    let emit_log = |lines: Vec<String>| {
        for l in lines {
            say(&l);
        }
        last.lock().unwrap().2 = Instant::now();
    };
    let end = setup::run(&shared, &p, jobs_of(&cfg), &emit, &emit_log);
    if end.state == "cancelled" {
        // in the window this is a pause the player chose; here nothing asked for it
        return fail("Set up stopped before it finished. See setup.log in the game's folder.");
    }
    if end.state != "done" {
        return fail(if end.message.trim().is_empty() { "Set up did not finish. See setup.log in the game's folder." } else { &end.message });
    }
    if !setup::complete(&p) {
        return fail("Set up ended, but the game is not ready. See setup.log in the game's folder.");
    }
    if let Err(e) = play_paths(&p) {
        return fail(&e);
    }
    progress(1.0, "Ready to play");
    0
}

/// What Play does before it starts ObeliskChamber.exe (ObeliskChamber.ini points at the game code, the dump, the mods and the caches),
/// with the paths inside the game folder written relative to it, so the folder can move (settings.rs relative_play_paths).
fn play_paths(p: &setup::Paths) -> Result<(), String> {
    settings::ensure_play_paths(&p.game_dir, p)?;
    settings::relative_play_paths(&p.game_dir, p)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(list: &[&str]) -> Vec<String> {
        list.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn prepare_is_found_anywhere_on_the_line() {
        assert!(wanted(&args(&["--game", "D:/dump", "--prepare"])));
        assert!(!wanted(&args(&["--game", "D:/dump"])));
    }

    #[test]
    fn a_value_follows_its_option() {
        assert_eq!(value(&args(&["--prepare", "--game", "D:/my dump"]), "--game").as_deref(), Some("D:/my dump"));
    }

    #[test]
    fn a_value_can_be_joined_with_equals() {
        assert_eq!(value(&args(&["--prepare", "--game=D:/dump.nup"]), "--game").as_deref(), Some("D:/dump.nup"));
    }

    #[test]
    fn an_empty_or_missing_value_is_none() {
        assert_eq!(value(&args(&["--prepare", "--game="]), "--game"), None);
        assert_eq!(value(&args(&["--prepare", "--game"]), "--game"), None);
        assert_eq!(value(&args(&["--prepare"]), "--game"), None);
    }

    #[test]
    fn a_similar_option_is_not_taken_for_it() {
        assert_eq!(value(&args(&["--gamedir=x"]), "--game"), None);
    }
}
