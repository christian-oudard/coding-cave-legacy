//! cav-package-manager: host-side daemon that fetches package
//! artifacts into a shared cache directory on behalf of clients
//! (typically the cav-package-client wrapper running inside a cave).
//!
//! The daemon never reads bytes from the cave. The client sends a list
//! of (url, sha256, filename) triples; the daemon fetches each from
//! upstream over the host's network, verifies the hash, and writes the
//! file to the cache dir using an atomic create-rename. The cave's
//! tool then finds the file in the cache via a bind mount and proceeds
//! offline.

use std::collections::HashSet;
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{Context, Result};

#[path = "../pkg.rs"]
mod pkg;
use pkg::{
    default_cache_dir, default_socket_path, read_message, write_message, Artifact, FetchRequest,
    FetchResponse, FetchResult, FetchStatus,
};

fn main() -> Result<()> {
    let socket_path = default_socket_path();
    let cache_dir = default_cache_dir();

    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::create_dir_all(&cache_dir)?;

    let _ = std::fs::remove_file(&socket_path);
    let listener = UnixListener::bind(&socket_path)
        .with_context(|| format!("bind socket {}", socket_path.display()))?;

    eprintln!(
        "cav-package-manager listening on {}",
        socket_path.display()
    );
    eprintln!("cache dir: {}", cache_dir.display());

    for conn in listener.incoming() {
        match conn {
            Ok(stream) => {
                let cache_dir = cache_dir.clone();
                std::thread::spawn(move || {
                    if let Err(e) = handle_client(stream, &cache_dir) {
                        eprintln!("client error: {e:#}");
                    }
                });
            }
            Err(e) => eprintln!("accept error: {e}"),
        }
    }
    Ok(())
}

fn handle_client(mut stream: UnixStream, cache_dir: &Path) -> Result<()> {
    let req: FetchRequest = read_message(&mut stream)?;
    eprintln!(
        "fetch request: tool={} artifacts={} requirements={}",
        req.tool,
        req.artifacts.len(),
        req.requirements.as_ref().map(|s| s.len()).unwrap_or(0),
    );

    if req.tool != "uv" {
        let resp = FetchResponse {
            results: vec![FetchResult {
                url: format!("tool:{}", req.tool),
                status: FetchStatus::Error {
                    message: format!("unsupported tool: {}", req.tool),
                },
            }],
        };
        write_message(&mut stream, &resp)?;
        return Ok(());
    }

    let mut results = Vec::new();

    // Path A: explicit artifacts. Useful when the caller knows URLs.
    let mut seen = HashSet::new();
    for art in &req.artifacts {
        if !seen.insert(art.url.clone()) {
            continue;
        }
        let status = fetch_one(art, cache_dir);
        results.push(FetchResult {
            url: art.url.clone(),
            status,
        });
    }

    // Path B: requirements file. Delegates to uv on the host to
    // resolve URLs, fetch, and verify hashes against the entries in
    // the requirements text. Result wheels land in `cache_dir`,
    // matching the layout the cave's uv expects to find via
    // --find-links.
    if let Some(req_text) = &req.requirements {
        match download_via_uv(req_text, cache_dir) {
            Ok(()) => {
                results.push(FetchResult {
                    url: "requirements:".into(),
                    status: FetchStatus::Fetched,
                });
            }
            Err(e) => {
                results.push(FetchResult {
                    url: "requirements:".into(),
                    status: FetchStatus::Error {
                        message: format!("{e:#}"),
                    },
                });
            }
        }
    }

    write_message(&mut stream, &FetchResponse { results })?;
    Ok(())
}

/// Run `pip download` against the given resolved-requirements text,
/// writing wheel/sdist artifacts into `cache_dir`. pip handles URL
/// resolution, fetching, and hash verification per the requirement
/// lines.
///
/// uv 0.11 doesn't have `pip download`, so we delegate to real pip
/// (or `python -m pip`) which does. Since the daemon runs host-side,
/// it has access to the host's normal Python toolchain.
fn download_via_uv(requirements_text: &str, cache_dir: &Path) -> Result<()> {
    let req_path = cache_dir.parent().unwrap_or(cache_dir).join(format!(
        ".cav-pkg-mgr-req-{}.txt",
        std::process::id()
    ));
    std::fs::write(&req_path, requirements_text)
        .with_context(|| format!("write requirements to {}", req_path.display()))?;

    // Try `pip download` first; fall back to `python -m pip download`.
    let pip_cmd = std::env::var("CAV_PKG_PIP")
        .unwrap_or_else(|_| "pip".to_string());

    let status = Command::new(&pip_cmd)
        .arg("download")
        .arg("--no-deps")
        .arg("--no-build-isolation")
        .arg("--dest")
        .arg(cache_dir)
        .arg("-r")
        .arg(&req_path)
        .status();

    let _ = std::fs::remove_file(&req_path);
    let status = status.context("spawn pip download")?;
    if !status.success() {
        anyhow::bail!("pip download exited with {status}");
    }
    Ok(())
}

/// Fetch a single artifact into the cache directory. Idempotent: if
/// the file already exists with a matching hash, no work is done.
fn fetch_one(art: &Artifact, cache_dir: &Path) -> FetchStatus {
    let target = cache_dir.join(&art.filename);

    // If the target exists with a matching hash, we're done.
    if target.is_file() {
        match sha256_file(&target) {
            Ok(s) if s == art.sha256 => return FetchStatus::Cached,
            Ok(s) => {
                eprintln!(
                    "cache file {} has wrong hash ({s} expected {}); re-fetching",
                    target.display(),
                    art.sha256
                );
            }
            Err(e) => eprintln!("hash check failed for {}: {e}", target.display()),
        }
    }

    // Fetch via curl; atomic write to a temp path then rename.
    let tmp = cache_dir.join(format!(".{}.partial", art.filename));
    if let Err(e) = curl_fetch(&art.url, &tmp) {
        let _ = std::fs::remove_file(&tmp);
        return FetchStatus::Error {
            message: format!("fetch failed: {e}"),
        };
    }

    let actual = match sha256_file(&tmp) {
        Ok(h) => h,
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            return FetchStatus::Error {
                message: format!("hash compute failed: {e}"),
            };
        }
    };
    if actual != art.sha256 {
        let _ = std::fs::remove_file(&tmp);
        return FetchStatus::Error {
            message: format!("hash mismatch: got {actual}, expected {}", art.sha256),
        };
    }

    if let Err(e) = std::fs::rename(&tmp, &target) {
        let _ = std::fs::remove_file(&tmp);
        return FetchStatus::Error {
            message: format!("rename failed: {e}"),
        };
    }
    FetchStatus::Fetched
}

fn curl_fetch(url: &str, dest: &Path) -> Result<()> {
    let status = Command::new("curl")
        .args([
            "--silent",
            "--show-error",
            "--fail",
            "--location",
            "--output",
        ])
        .arg(dest)
        .arg(url)
        .status()
        .with_context(|| format!("spawn curl for {url}"))?;
    if !status.success() {
        anyhow::bail!("curl exited with {status}");
    }
    Ok(())
}

/// Compute SHA-256 of a file using the sha256sum CLI. Avoids pulling
/// in a hashing crate for the prototype.
fn sha256_file(path: &Path) -> Result<String> {
    let out = Command::new("sha256sum")
        .arg(path)
        .output()
        .with_context(|| format!("spawn sha256sum for {}", path.display()))?;
    if !out.status.success() {
        anyhow::bail!(
            "sha256sum failed: {}",
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let s = String::from_utf8_lossy(&out.stdout);
    let hex = s.split_whitespace().next().unwrap_or("").to_string();
    if hex.len() != 64 {
        anyhow::bail!("unexpected sha256sum output: {s}");
    }
    Ok(hex)
}

#[allow(dead_code)]
fn unused_path_marker(_: PathBuf) {
    // Silence dead_code for PathBuf import in case it's unused above.
    // Keeping the import explicit for readers.
}
