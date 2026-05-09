use std::io::IsTerminal;
use std::sync::OnceLock;

fn enabled() -> bool {
    static ENABLED: OnceLock<bool> = OnceLock::new();
    *ENABLED.get_or_init(|| {
        if std::env::var_os("NO_COLOR").is_some() {
            return false;
        }
        if std::env::var("CLICOLOR_FORCE")
            .map(|v| v != "0")
            .unwrap_or(false)
        {
            return true;
        }
        std::io::stdout().is_terminal()
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
pub fn bold_white(s: &str) -> String { wrap(s, "1;97") }
