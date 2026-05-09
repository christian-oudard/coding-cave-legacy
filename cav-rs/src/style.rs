//! ANSI color helpers for cav's terminal output.
//!
//! # Style guide
//!
//! Identifiers
//! - Project slug          → [`blue`]        (e.g. `coding-cave`)
//! - Agent suffix `@N`     → [`bold_blue`]   (the differentiator within `slug@N`)
//! - Branch name           → [`yellow`]      (e.g. `agent-3/main`)
//! - Commit SHA            → [`yellow`]      (also a ref)
//! - File paths            → plain
//!
//! State
//! - Running               → [`green`]; `●` [`bold_green`]
//! - Stopped               → [`dim`];   `○` [`dim`]
//! - Time-ago, secondary   → [`dim`]
//!
//! Severity
//! - Error message body    → [`red`]         (real problems)
//! - "Warning:" prefix     → [`bold_yellow`] (confirmation prompts)
//! - Sync arrows ↑ ↓ ↕     → [`bold_yellow`]
//! - Benign markers `*`    → plain
//!
//! Emphasis
//! - Numeric counts        → [`bold`]        (no color)
//! - Section headers       → [`bold_white`]  (e.g. `=== slug ===`)
//!
//! Action hints (commands the user should type)
//! - `cav push` / `cav pull` / `cav agent` / etc. → [`bold_cyan`]
//!
//! Prompts
//! - `Approve? [y/N]`      → plain
//!
//! Color is on when stdout is a TTY and `NO_COLOR` is unset (per
//! https://no-color.org — any non-empty value disables).

use std::io::IsTerminal;
use std::sync::OnceLock;

/// Pure decision function — separated from env/TTY reads so it can be
/// unit-tested deterministically (the public `enabled()` caches via
/// OnceLock and can't be reset).
fn decide(no_color: Option<&str>, stdout_tty: bool) -> bool {
    if no_color.is_some_and(|v| !v.is_empty()) {
        return false;
    }
    stdout_tty
}

fn enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| {
        decide(
            std::env::var("NO_COLOR").ok().as_deref(),
            std::io::stdout().is_terminal(),
        )
    })
}

const RESET: &str = "\x1b[0m";

fn wrap(s: &str, code: &str) -> String {
    if enabled() {
        format!("\x1b[{code}m{s}{RESET}")
    } else {
        s.to_string()
    }
}

pub fn bold(s: &str) -> String { wrap(s, "1") }
pub fn dim(s: &str) -> String { wrap(s, "2") }
pub fn red(s: &str) -> String { wrap(s, "31") }
pub fn green(s: &str) -> String { wrap(s, "32") }
pub fn yellow(s: &str) -> String { wrap(s, "33") }
pub fn blue(s: &str) -> String { wrap(s, "34") }
pub fn bold_blue(s: &str) -> String { wrap(s, "1;34") }
pub fn bold_cyan(s: &str) -> String { wrap(s, "1;36") }
pub fn bold_yellow(s: &str) -> String { wrap(s, "1;33") }
pub fn bold_green(s: &str) -> String { wrap(s, "1;32") }
pub fn bold_red(s: &str) -> String { wrap(s, "1;31") }
pub fn bold_white(s: &str) -> String { wrap(s, "1;97") }

/// Color a "<sha> <subject>" commit line: SHA yellow, subject default.
pub fn commit_line(line: &str) -> String {
    match line.split_once(' ') {
        Some((sha, subject)) => format!("{} {subject}", yellow(sha)),
        None => line.to_string(),
    }
}

/// Color an agent display name "<slug>@<id>": slug blue, "@id" bold blue.
pub fn agent_name(name: &str) -> String {
    match name.rsplit_once('@') {
        Some((slug, suffix)) => format!("{}{}", blue(slug), bold_blue(&format!("@{suffix}"))),
        None => blue(name),
    }
}

#[cfg(test)]
mod tests {
    use super::decide;

    #[test]
    fn tty_default_on() {
        assert!(decide(None, true));
    }

    #[test]
    fn pipe_default_off() {
        assert!(!decide(None, false));
    }

    #[test]
    fn no_color_set_disables_even_on_tty() {
        assert!(!decide(Some("1"), true));
        assert!(!decide(Some("anything"), true));
    }

    #[test]
    fn no_color_empty_does_not_disable() {
        // Per https://no-color.org: only non-empty values count as set.
        assert!(decide(Some(""), true));
    }
}
