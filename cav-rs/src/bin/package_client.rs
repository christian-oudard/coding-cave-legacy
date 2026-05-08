//! cav-package-client: thin wrapper that intercepts package-manager
//! invocations from inside a cave.
//!
//! Symlinked under names like `uv` and `pip` on the cave's PATH; argv[0]
//! determines which tool is being wrapped. For each supported tool the
//! wrapper:
//!   1. Runs a resolve-only invocation of the real tool.
//!   2. Parses the resolved package list (URL + hash) from the output.
//!   3. Sends the list to cav-package-manager over a unix socket.
//!   4. Daemon fetches each artifact into the shared cache directory.
//!   5. Runs the real tool against the populated cache, offline.
//!
//! For commands that don't need the resolve/install split (e.g.,
//! `uv venv`, `uv --version`), the wrapper just forwards to the real
//! tool directly.
//!
//! First version handles uv only. Cargo, npm, etc. follow the same
//! pattern with per-tool resolvers.

use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

use anyhow::{Context, Result};

#[path = "../pkg.rs"]
mod pkg;
use pkg::{
    default_cache_dir, default_socket_path, read_message, write_message, FetchRequest,
    FetchResponse, FetchStatus,
};

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().collect();
    let tool = match tool_from_argv0(&argv[0]) {
        Some(t) => t,
        None => {
            eprintln!(
                "cav-package-client: invoked under unknown name {} \
                 (must be symlinked as uv, pip, etc., or invoked as \
                 'cav-package-client <tool> ...')",
                argv[0]
            );
            return ExitCode::from(2);
        }
    };

    let tool_args: Vec<String> = if Path::new(&argv[0])
        .file_name()
        .and_then(|s| s.to_str())
        == Some("cav-package-client")
    {
        // Explicit form: cav-package-client uv pip install foo
        argv.iter().skip(2).cloned().collect()
    } else {
        // Symlinked form: uv pip install foo
        argv.iter().skip(1).cloned().collect()
    };

    match dispatch(&tool, &tool_args) {
        Ok(code) => ExitCode::from(code),
        Err(e) => {
            eprintln!("cav-package-client: {e:#}");
            ExitCode::from(1)
        }
    }
}

fn tool_from_argv0(argv0: &str) -> Option<String> {
    let basename = Path::new(argv0)
        .file_name()
        .and_then(|s| s.to_str())?
        .to_string();
    if basename == "cav-package-client" {
        // Explicit-form: tool name is argv[1]
        std::env::args().nth(1)
    } else {
        Some(basename)
    }
}

fn dispatch(tool: &str, args: &[String]) -> Result<u8> {
    match tool {
        "uv" => uv_dispatch(args),
        // Stub for now; pip falls through to whatever's on PATH.
        // Real pip wrapping is a follow-up.
        other => {
            anyhow::bail!(
                "cav-package-client: tool '{other}' not yet wrapped. \
                 Add a handler in cav-rs/src/bin/package_client.rs."
            );
        }
    }
}

// ---- uv handler ----

fn uv_dispatch(args: &[String]) -> Result<u8> {
    let real_uv = real_binary("uv")?;

    // Decide whether this invocation needs the resolve-then-fetch dance.
    // First version: only `uv pip install <pkgs>` and `uv pip sync`.
    // Everything else passes straight through to real uv.
    let plan = classify_uv_command(args);
    match plan {
        UvCommand::PipInstall { packages, requirements_file } => {
            run_uv_pip_install(&real_uv, args, packages.as_deref(), requirements_file.as_deref())
        }
        UvCommand::Passthrough => passthrough(&real_uv, args),
    }
}

#[derive(Debug)]
enum UvCommand {
    PipInstall {
        packages: Option<Vec<String>>,
        requirements_file: Option<PathBuf>,
    },
    Passthrough,
}

/// Recognize the subset of `uv` commands the wrapper handles. Returns
/// Passthrough for anything not in the recognized set; the wrapper will
/// just exec the real tool. This keeps the wrapper conservative —
/// unrecognized commands still work, they just don't get the
/// daemon-managed cache benefit yet.
fn classify_uv_command(args: &[String]) -> UvCommand {
    // Only `uv pip install ...` and `uv pip sync ...` for now.
    let mut iter = args.iter().skip_while(|a| a.starts_with('-'));
    let Some(first) = iter.next() else {
        return UvCommand::Passthrough;
    };
    if first != "pip" {
        return UvCommand::Passthrough;
    }
    let Some(second) = iter.next() else {
        return UvCommand::Passthrough;
    };
    if second != "install" && second != "sync" {
        return UvCommand::Passthrough;
    }

    let mut packages = Vec::new();
    let mut req_file: Option<PathBuf> = None;
    let mut peekable = iter.peekable();
    while let Some(arg) = peekable.next() {
        if arg == "-r" || arg == "--requirements" || arg == "--requirement" {
            if let Some(path) = peekable.next() {
                req_file = Some(PathBuf::from(path));
            }
        } else if let Some(rest) = arg.strip_prefix("--requirement=") {
            req_file = Some(PathBuf::from(rest));
        } else if !arg.starts_with('-') {
            packages.push(arg.clone());
        }
    }

    UvCommand::PipInstall {
        packages: if packages.is_empty() { None } else { Some(packages) },
        requirements_file: req_file,
    }
}

fn run_uv_pip_install(
    real_uv: &Path,
    original_args: &[String],
    packages: Option<&[String]>,
    requirements_file: Option<&Path>,
) -> Result<u8> {
    // Phase 1: resolve via `uv pip compile`. Output a fully pinned
    // requirements file with --generate-hashes so we have URLs (or at
    // least hashes) to hand to the daemon.
    let resolved_path = std::env::temp_dir().join(format!(
        "cav-pkg-resolved-{}.txt",
        std::process::id()
    ));

    // uv pip compile takes requirements files, not package specs
    // directly. If the caller passed package args, write them to a
    // temp file first.
    let inline_req_file = if let Some(pkgs) = packages {
        let p = std::env::temp_dir().join(format!(
            "cav-pkg-input-{}.txt",
            std::process::id()
        ));
        let body = pkgs.join("\n");
        std::fs::write(&p, body)
            .with_context(|| format!("write requirements input to {}", p.display()))?;
        Some(p)
    } else {
        None
    };

    let mut compile_cmd = Command::new(real_uv);
    compile_cmd
        .arg("pip")
        .arg("compile")
        .arg("--quiet")
        .arg("--generate-hashes")
        .arg("--output-file")
        .arg(&resolved_path);
    if let Some(file) = requirements_file {
        compile_cmd.arg(file);
    }
    if let Some(p) = &inline_req_file {
        compile_cmd.arg(p);
    }

    let status = compile_cmd
        .status()
        .context("spawn uv pip compile")?;
    if let Some(p) = &inline_req_file {
        let _ = std::fs::remove_file(p);
    }
    if !status.success() {
        let code = status.code().unwrap_or(1) as u8;
        return Ok(code);
    }

    // Phase 2: parse resolved.txt for (filename, sha256) pairs.
    // Looking up URLs requires either querying the index again or
    // having uv emit them. For the first cut, fetch URLs from the
    // index ourselves via uv's --no-deps download path. Simpler:
    // just hand requirements + hashes to the daemon and let the
    // daemon's uv resolve the URL.
    //
    // BUT: that re-introduces a uv-on-the-host with full resolve.
    // Cleaner first cut: have the daemon accept (filename, sha256)
    // plus the requirements line, and the daemon does its own fetch
    // by querying PyPI's simple API.
    //
    // For this prototype, simplification: we send the resolved file
    // path itself and ask the daemon to fetch by re-running
    // `uv pip download` against it. That delegates layout knowledge
    // to uv on the host side. Trade-off: the daemon's ability to
    // respond to a single-package query is limited.
    //
    // (Full URL extraction from --generate-hashes output is doable
    // but requires re-querying the simple index. Out of scope for
    // the first prototype.)

    // Send the resolved-requirements text to the daemon. The daemon
    // hands it to uv pip download on the host, which resolves URLs
    // and writes wheels into the shared cache dir. Hashes in the
    // requirements text are verified by uv during download.
    let resolved_text = std::fs::read_to_string(&resolved_path)
        .with_context(|| format!("read {}", resolved_path.display()))?;
    if !resolved_text.trim().is_empty() {
        let resp = send_fetch_request(&resolved_text)?;
        let mut had_error = false;
        for r in &resp.results {
            if let FetchStatus::Error { message } = &r.status {
                eprintln!("cav-package-manager: {message}");
                had_error = true;
            }
        }
        if had_error {
            return Ok(1);
        }
    }

    // Phase 3: install offline against the populated cache.
    let cache_dir = default_cache_dir();
    let mut install_cmd = Command::new(real_uv);
    install_cmd
        .arg("pip")
        .arg("install")
        .arg("--no-index")
        .arg("--find-links")
        .arg(&cache_dir)
        .arg("--require-hashes")
        .arg("-r")
        .arg(&resolved_path);

    // Carry over user-specified flags from the original invocation
    // that affect targeting (--python, --target, etc.). Conservative
    // approach: scan for flags we know about; everything else is left
    // out to keep this prototype focused.
    forward_known_flags(&mut install_cmd, original_args);

    let status = install_cmd
        .status()
        .context("spawn uv pip install --no-index")?;
    Ok(status.code().unwrap_or(1) as u8)
}

fn send_fetch_request(requirements_text: &str) -> Result<FetchResponse> {
    let socket_path = default_socket_path();
    let mut stream = UnixStream::connect(&socket_path).with_context(|| {
        format!(
            "connect to cav-package-manager at {} (is the daemon running?)",
            socket_path.display()
        )
    })?;
    let req = FetchRequest {
        tool: "uv".to_string(),
        artifacts: Vec::new(),
        requirements: Some(requirements_text.to_string()),
    };
    write_message(&mut stream, &req)?;
    let resp: FetchResponse = read_message(&mut stream)?;
    Ok(resp)
}

fn forward_known_flags(cmd: &mut Command, original: &[String]) {
    let mut i = 0;
    while i < original.len() {
        let a = &original[i];
        let take_value = |idx: usize| original.get(idx + 1).cloned();
        match a.as_str() {
            "--python" | "-p" | "--target" | "--prefix" => {
                if let Some(v) = take_value(i) {
                    cmd.arg(a).arg(v);
                    i += 2;
                    continue;
                }
            }
            _ => {}
        }
        i += 1;
    }
}

// ---- Generic helpers ----

fn passthrough(real: &Path, args: &[String]) -> Result<u8> {
    let status = Command::new(real)
        .args(args)
        .status()
        .with_context(|| format!("spawn {}", real.display()))?;
    Ok(status.code().unwrap_or(1) as u8)
}

/// Find the real binary by name, skipping the wrapper's own location.
/// The wrapper is installed earliest in PATH; the real binary lives
/// further down. This is a simple version that tries common nix-store
/// paths and PATH; for production we'd want CODING_CAVE_REAL_<TOOL>
/// env vars set by cave-system.nix pinning the exact store path.
fn real_binary(name: &str) -> Result<PathBuf> {
    // Allow override via env for tests and explicit configuration.
    let envvar = format!("CAV_REAL_{}", name.to_uppercase());
    if let Ok(p) = std::env::var(&envvar) {
        return Ok(PathBuf::from(p));
    }

    // Read PATH and find an entry that's NOT this wrapper. The
    // wrapper's own path is argv[0] resolved.
    let self_path = std::env::current_exe().ok();
    let path_var = std::env::var("PATH").unwrap_or_default();
    for dir in path_var.split(':') {
        let candidate = Path::new(dir).join(name);
        if !candidate.is_file() {
            continue;
        }
        // Skip if this candidate is the wrapper itself (resolves to
        // the same canonical path).
        if let (Some(self_p), Ok(cand_canon)) =
            (&self_path, candidate.canonicalize())
        {
            if let Ok(self_canon) = self_p.canonicalize() {
                if cand_canon == self_canon {
                    continue;
                }
            }
        }
        return Ok(candidate);
    }
    anyhow::bail!(
        "could not find real {name} on PATH (set {envvar} to the absolute path)"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classify_pip_install_with_packages() {
        let args = vec!["pip", "install", "requests", "click"]
            .into_iter()
            .map(String::from)
            .collect::<Vec<_>>();
        match classify_uv_command(&args) {
            UvCommand::PipInstall { packages, .. } => {
                assert_eq!(packages, Some(vec!["requests".into(), "click".into()]));
            }
            other => panic!("expected PipInstall, got {other:?}"),
        }
    }

    #[test]
    fn classify_pip_install_with_requirements_file() {
        let args = vec!["pip", "install", "-r", "/tmp/reqs.txt"]
            .into_iter()
            .map(String::from)
            .collect::<Vec<_>>();
        match classify_uv_command(&args) {
            UvCommand::PipInstall { requirements_file, packages } => {
                assert_eq!(requirements_file, Some(PathBuf::from("/tmp/reqs.txt")));
                assert!(packages.is_none());
            }
            other => panic!("expected PipInstall, got {other:?}"),
        }
    }

    #[test]
    fn classify_unknown_command_passes_through() {
        let args = vec!["venv".to_string()];
        match classify_uv_command(&args) {
            UvCommand::Passthrough => {}
            other => panic!("expected Passthrough, got {other:?}"),
        }
    }

}
