use std::io::{self, Read};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::exit;

const MAX_PKT_LEN: usize = 65520;

fn parse_request(data: &[u8]) -> Option<(String, String)> {
    let parts: Vec<&[u8]> = data.splitn(2, |&b| b == 0).collect();
    let request = std::str::from_utf8(parts.first()?).ok()?;
    let mut pieces = request.splitn(2, ' ');
    let service = pieces.next()?.to_string();
    let path = pieces.next()?.to_string();
    Some((service, path))
}

fn validate_service(service: &str) -> bool {
    service == "git-upload-pack" || service == "git-receive-pack"
}

fn resolve_repo(base_path: &str, request_path: &str) -> Option<String> {
    let cleaned = Path::new(request_path.trim_start_matches('/'));
    // Reject any ".." component
    for comp in cleaned.components() {
        if comp.as_os_str() == ".." {
            return None;
        }
    }
    // Check path (before resolving symlinks) is within base_path
    let candidate = Path::new(base_path).join(cleaned);
    if !candidate.starts_with(base_path) {
        return None;
    }
    // Resolve symlinks (daemon uses symlinks pointing to actual bare repos)
    let resolved = std::fs::canonicalize(&candidate).ok()?;
    Some(resolved.to_string_lossy().to_string())
}

fn main() {
    let agent_id = std::env::var("CODING_CAVE_HANDLER_AGENT_ID").unwrap_or_default();
    let base_path = std::env::var("CODING_CAVE_HANDLER_BASE_PATH").unwrap_or_default();

    if agent_id.is_empty() || base_path.is_empty() {
        exit(1);
    }

    unsafe { std::env::set_var("CODING_CAVE_AGENT_ID", &agent_id) };

    let mut stdin = io::stdin();

    // Read 4-byte pkt-line header
    let mut header = [0u8; 4];
    if stdin.read_exact(&mut header).is_err() {
        exit(0);
    }

    let hex = match std::str::from_utf8(&header) {
        Ok(s) => s,
        Err(_) => exit(1),
    };
    let pkt_len = match usize::from_str_radix(hex, 16) {
        Ok(n) => n,
        Err(_) => exit(1),
    };

    if pkt_len < 4 || pkt_len > MAX_PKT_LEN {
        exit(1);
    }

    let mut payload = vec![0u8; pkt_len - 4];
    if stdin.read_exact(&mut payload).is_err() {
        exit(1);
    }

    let (service, path) = match parse_request(&payload) {
        Some(p) => p,
        None => exit(1),
    };

    if !validate_service(&service) {
        exit(1);
    }

    let repo = match resolve_repo(&base_path, &path) {
        Some(r) => r,
        None => exit(1),
    };

    let err = std::process::Command::new(&service)
        .arg(&repo)
        .exec();
    eprintln!("exec failed: {err}");
    exit(1);
}
