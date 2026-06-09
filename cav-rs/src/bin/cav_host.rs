// cav-host: perform a host-facing action from inside a cave.
//
// `bell` rings the user's terminal. Inside a cave the terminal is the
// supervisor's stdout (PID 1 in the cave's pid namespace), reachable at
// /proc/1/fd/1 — this binary encapsulates that path so hooks and cave.nix do
// not hardcode it. Best-effort: a no-op when no tty is attached (e.g. a
// detached agent). Richer actions that genuinely need the host (desktop
// notify, clipboard) will later go over a host socket; bell needs no round-trip.
//
// `version` prints the version of the running cave, read from the
// CODING_CAVE_VERSION the sandbox injects at launch.

use std::io::Write;

// PID 1 in a cave is cav-supervisor; its stdout is the launching terminal.
const TERMINAL: &str = "/proc/1/fd/1";

fn main() {
    match std::env::args().nth(1).as_deref() {
        Some("bell") => bell(),
        Some("version") => version(),
        other => {
            eprintln!("usage: cav-host <bell|version> (got {other:?})");
            std::process::exit(1);
        }
    }
}

fn bell() {
    if let Ok(mut term) = std::fs::OpenOptions::new().write(true).open(TERMINAL) {
        let _ = term.write_all(b"\x07");
    }
}

fn version() {
    match std::env::var("CODING_CAVE_VERSION") {
        Ok(v) if !v.is_empty() => println!("cave {v}"),
        _ => {
            eprintln!("cav-host: not running inside a cave (CODING_CAVE_VERSION unset)");
            std::process::exit(1);
        }
    }
}
