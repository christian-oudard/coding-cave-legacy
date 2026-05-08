//! Protocol types for the package-manager daemon and its clients.
//!
//! Wire format: length-prefixed JSON. Each message is a u32 big-endian
//! length followed by that many bytes of UTF-8 JSON. Both directions
//! use the same framing.
//!
//! Flow:
//!     client → daemon: FetchRequest { tool, artifacts: [{url, sha256, filename}, ...] }
//!     daemon → client: FetchResponse { results: [{url, status, error?}, ...] }
//!
//! The daemon's job is to ensure each artifact is present in the
//! shared host-side cache directory it was configured with. The client
//! then runs the underlying tool offline, finding the artifacts via
//! the configured --find-links / --offline path.

use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use std::os::unix::net::UnixStream;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Artifact {
    pub url: String,
    pub sha256: String,
    pub filename: String,
}

/// A request to populate the shared cache. Two shapes supported:
/// - `requirements`: a verbatim resolved-requirements file (uv pip
///   compile --generate-hashes output). The daemon runs the underlying
///   tool to download, leveraging its native resolver and URL lookup.
/// - `artifacts`: an explicit list of (url, sha256, filename) triples.
///   The daemon fetches each directly. Useful when the client already
///   knows URLs or for non-uv tools.
///
/// First version of the prototype implements the requirements path
/// for uv. The artifacts path is the more general future shape.
#[derive(Debug, Serialize, Deserialize)]
pub struct FetchRequest {
    pub tool: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub artifacts: Vec<Artifact>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub requirements: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(rename_all = "lowercase", tag = "status")]
pub enum FetchStatus {
    /// Already in cache; no fetch performed.
    Cached,
    /// Fetched from upstream and verified.
    Fetched,
    /// Hash mismatch, network error, or other failure.
    Error { message: String },
}

#[derive(Debug, Serialize, Deserialize)]
pub struct FetchResult {
    pub url: String,
    #[serde(flatten)]
    pub status: FetchStatus,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct FetchResponse {
    pub results: Vec<FetchResult>,
}

pub fn write_message<T: Serialize>(stream: &mut UnixStream, msg: &T) -> std::io::Result<()> {
    let bytes = serde_json::to_vec(msg)?;
    let len = u32::try_from(bytes.len())
        .map_err(|_| std::io::Error::other("message too large"))?;
    stream.write_all(&len.to_be_bytes())?;
    stream.write_all(&bytes)?;
    stream.flush()?;
    Ok(())
}

pub fn read_message<T: for<'de> Deserialize<'de>>(stream: &mut UnixStream) -> std::io::Result<T> {
    let mut len_buf = [0u8; 4];
    stream.read_exact(&mut len_buf)?;
    let len = u32::from_be_bytes(len_buf) as usize;
    if len > 16 * 1024 * 1024 {
        return Err(std::io::Error::other("message too large"));
    }
    let mut buf = vec![0u8; len];
    stream.read_exact(&mut buf)?;
    serde_json::from_slice(&buf).map_err(std::io::Error::other)
}

/// Default socket path for the package manager daemon.
pub fn default_socket_path() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("CODING_CAVE_PACKAGE_MANAGER_SOCK") {
        return std::path::PathBuf::from(p);
    }
    if let Ok(home) = std::env::var("HOME") {
        return std::path::PathBuf::from(home)
            .join(".local/share/coding-cave/package-manager/sock");
    }
    std::path::PathBuf::from("/tmp/cav-package-manager.sock")
}

/// Default cache directory the daemon writes into. The wrapper passes
/// this as --find-links to uv (and similar to other tools later).
pub fn default_cache_dir() -> std::path::PathBuf {
    if let Ok(p) = std::env::var("CODING_CAVE_PACKAGE_CACHE_DIR") {
        return std::path::PathBuf::from(p);
    }
    if let Ok(home) = std::env::var("HOME") {
        return std::path::PathBuf::from(home)
            .join(".local/share/coding-cave/package-cache");
    }
    std::path::PathBuf::from("/tmp/cav-package-cache")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fetch_request_roundtrips_through_json() {
        let req = FetchRequest {
            tool: "uv".to_string(),
            artifacts: vec![Artifact {
                url: "https://files.pythonhosted.org/.../foo.whl".to_string(),
                sha256: "abc123".to_string(),
                filename: "foo-1.0.0-py3-none-any.whl".to_string(),
            }],
            requirements: None,
        };
        let json = serde_json::to_string(&req).unwrap();
        let parsed: FetchRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.tool, "uv");
        assert_eq!(parsed.artifacts.len(), 1);
        assert_eq!(parsed.artifacts[0].sha256, "abc123");
    }

    #[test]
    fn fetch_response_serializes_status_variants() {
        let resp = FetchResponse {
            results: vec![
                FetchResult {
                    url: "u1".to_string(),
                    status: FetchStatus::Fetched,
                },
                FetchResult {
                    url: "u2".to_string(),
                    status: FetchStatus::Cached,
                },
                FetchResult {
                    url: "u3".to_string(),
                    status: FetchStatus::Error {
                        message: "boom".to_string(),
                    },
                },
            ],
        };
        let json = serde_json::to_string(&resp).unwrap();
        let parsed: FetchResponse = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed.results.len(), 3);
        assert!(matches!(parsed.results[0].status, FetchStatus::Fetched));
        assert!(matches!(parsed.results[1].status, FetchStatus::Cached));
        match &parsed.results[2].status {
            FetchStatus::Error { message } => assert_eq!(message, "boom"),
            _ => panic!("expected error variant"),
        }
    }
}
