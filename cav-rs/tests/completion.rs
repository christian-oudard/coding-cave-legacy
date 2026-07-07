//! End-to-end shell-completion tests for bash, zsh, and fish.
//!
//! The Rust `complete()` helpers are unit-tested elsewhere and return the
//! right candidate, but a completion can still break in the emitted shell
//! glue: `cav cp cave:` expands to `coding-cave@2:`, a candidate that
//! deliberately does NOT share the typed prefix, and each shell mishandles
//! that differently (zsh's compadd drops it without `-U`; bash splits the
//! word on the `:` in COMP_WORDBREAKS). The only faithful test is to drive
//! each shell through a real pty and inspect the command-line buffer after
//! pressing TAB. zsh's `zpty` module is the pty driver for all three.

use std::path::Path;
use std::process::Command;

const CAV: &str = env!("CARGO_BIN_EXE_cav");

fn bin_dir() -> String {
    Path::new(CAV).parent().unwrap().display().to_string()
}

fn path_with_bin() -> String {
    match std::env::var("PATH") {
        Ok(p) => format!("{}:{}", bin_dir(), p),
        Err(_) => bin_dir(),
    }
}

fn have(cmd: &str) -> bool {
    Command::new(cmd)
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// A zsh driver script: spawn the target shell under a `zpty` pty, load
/// cav's completion, type the line, press TAB, then bind Ctrl-T to print the
/// resulting command-line buffer between `@@` markers. Each shell reads its
/// live line buffer differently ($BUFFER / $READLINE_LINE / commandline).
const HARNESS: &str = r#"
zmodload zsh/zpty
local shell=$1 pdir=$2 line=$3
case $shell in
  (zsh)
    zpty ZS 'zsh -f'
    zpty -w ZS "stty -echo -ixon; cd $pdir"
    zpty -w ZS "autoload -Uz compinit; compinit -C -d \$PWD/.zcompdump; eval \"\$(cav completion zsh)\""
    zpty -w ZS 'dumpbuf(){ print -rn -- "@@${BUFFER}@@" }; zle -N dumpbuf; bindkey "^T" dumpbuf; print DONE_SETUP'
    ;;
  (bash)
    zpty ZS 'bash --norc --noprofile -i'
    zpty -w ZS "stty -echo -ixon; cd $pdir; eval \"\$(cav completion bash)\""
    zpty -w ZS 'bind -x '\''"\C-t": printf "@@%s@@" "$READLINE_LINE"'\''; echo DONE_SETUP'
    ;;
  (fish)
    zpty ZS 'fish --no-config --private'
    zpty -w ZS "stty -echo -ixon; cd $pdir; cav completion fish | source"
    zpty -w ZS 'function _db; printf "@@%s@@" (commandline); end; bind \ct _db; echo DONE_SETUP'
    ;;
esac
local x; zpty -r -m ZS x '*DONE_SETUP*' 2>/dev/null
zpty -w -n ZS "$line"
zpty -w -n ZS $'\t'
# Let completion settle before dumping the buffer (fish computes async).
zmodload zsh/zselect 2>/dev/null && zselect -t 50
zpty -w -n ZS $'\024'
local y; zpty -r -m ZS y '*@@*@@*' 2>/dev/null
zpty -d ZS
local buf="${y#*@@}"; buf="${buf%%@@*}"
print -r -- "BUFFER=[$buf]"
"#;

struct Fixture {
    dir: std::path::PathBuf,
    data_dir: std::path::PathBuf,
    harness: std::path::PathBuf,
    slug: String,
    _agent: Child,
}

/// A live process whose pid stands in for a running agent; killed on drop.
struct Child(std::process::Child);
impl Drop for Child {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

impl Fixture {
    fn new(name: &str) -> Self {
        let uniq = format!("{}-{name}", std::process::id());
        let tmp = std::env::temp_dir();
        // Project dir name must survive project_slug() unchanged so the
        // registered slug matches the cwd-derived one.
        let slug = format!("cavcomp{uniq}");
        let dir = tmp.join(&slug);
        let data_dir = tmp.join(format!("cavcomp-data-{uniq}"));
        let harness = tmp.join(format!("cavcomp-harness-{uniq}.zsh"));
        let _ = std::fs::remove_dir_all(&dir);
        let _ = std::fs::remove_dir_all(&data_dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::create_dir_all(&data_dir).unwrap();
        std::fs::write(&harness, HARNESS).unwrap();

        // Materialize the registry DB (open() creates the schema) by running
        // the completion serving path once against this data dir.
        Command::new(CAV)
            .args(["--", "cav", "cp", "x"])
            .current_dir(&dir)
            .env("PATH", path_with_bin())
            .env("CODING_CAVE_DATA_DIR", &data_dir)
            .env("CODING_CAVE_NESTED", "1")
            .env("COMPLETE", "zsh")
            .env("_CLAP_COMPLETE_INDEX", "2")
            .env("_CLAP_IFS", "\n")
            .output()
            .expect("cav completion callback failed");

        // A live pid stands in for a running agent.
        let agent = Child(Command::new("sleep").arg("3600").spawn().expect("spawn sleep"));

        // Register the project and a running agent 2 directly in the DB.
        let sql = format!(
            "INSERT INTO projects (slug, path) VALUES ('{slug}', '{}');\n\
             INSERT INTO agents (agent_id, uuid, project, host_pid, started_at) \
             VALUES ('2', 'uuid-{uniq}', '{slug}', {}, 1.0);",
            dir.display(),
            agent.0.id(),
        );
        let out = Command::new("sqlite3")
            .arg(data_dir.join("coding-cave.db"))
            .arg(&sql)
            .output()
            .expect("sqlite3 not found");
        assert!(out.status.success(), "seed sql failed: {}", String::from_utf8_lossy(&out.stderr));

        Self { dir, data_dir, harness, slug, _agent: agent }
    }

    /// Type `line` in `shell`, press TAB, return the resulting buffer.
    fn complete(&self, shell: &str, line: &str) -> String {
        let out = Command::new("timeout")
            .args(["60", "zsh", "-f"])
            .arg(&self.harness)
            .arg(shell)
            .arg(&self.dir)
            .arg(line)
            .env("PATH", path_with_bin())
            .env("CODING_CAVE_DATA_DIR", &self.data_dir)
            .env("CODING_CAVE_NESTED", "1")
            .output()
            .expect("zsh harness failed to run");
        let stdout = String::from_utf8_lossy(&out.stdout);
        let line = stdout
            .lines()
            .find(|l| l.starts_with("BUFFER=["))
            .unwrap_or_else(|| panic!("no BUFFER line in harness output: {stdout}"));
        line.trim_start_matches("BUFFER=[").trim_end_matches(']').to_string()
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
        let _ = std::fs::remove_dir_all(&self.data_dir);
        let _ = std::fs::remove_file(&self.harness);
    }
}

fn precheck(shell: &str) -> bool {
    // The driver is always zsh; the target shell and sqlite must also exist.
    if !have("zsh") || !have("sqlite3") || !have(shell) {
        eprintln!("skipping {shell}: zsh/sqlite3/{shell} not on PATH");
        return false;
    }
    true
}

/// The `cave` keyword shorthand expands to the fully-qualified
/// `<slug>@<agent>:` form. The candidate shares no prefix with the typed
/// `cave` / `cave:`, so this only works where the shell force-replaces the
/// word: zsh via `compadd -U`, bash once `:` is out of COMP_WORDBREAKS.
/// (fish can't: it fuzzy-filters candidates against the token with no
/// override, so a non-matching slug is dropped. See `fish_completes_agent_qualifier`.)
fn assert_shorthand_expands(shell: &str) {
    if !precheck(shell) {
        return;
    }
    let fx = Fixture::new(shell);
    let want = format!("cav cp {}@2:", fx.slug);
    assert_eq!(fx.complete(shell, "cav cp cave:"), want, "{shell}: `cave:` should expand");
    assert_eq!(fx.complete(shell, "cav cp cave"), want, "{shell}: `cave` should expand");
}

#[test]
fn zsh_expands_cave_shorthand() {
    assert_shorthand_expands("zsh");
}

#[test]
fn bash_expands_cave_shorthand() {
    assert_shorthand_expands("bash");
}

// fish fuzzy-filters candidates against the typed token, so the `cave`
// keyword shorthand only surfaces when it matches the slug — a limitation we
// can't override. What fish must still do is complete a real slug prefix to
// the agent-qualified form; guard that.
#[test]
fn fish_completes_agent_qualifier() {
    if !precheck("fish") {
        return;
    }
    let fx = Fixture::new("fish");
    let typed = format!("cav cp {}", fx.slug);
    let want = format!("cav cp {}@2:", fx.slug);
    assert_eq!(fx.complete("fish", &typed), want, "fish: slug prefix should gain @<agent>:");
}

// A prefix-matching word must complete without a trailing space, so the next
// TAB descends into the agent's worktree instead of inserting a blank. (zsh
// specific: it lost `-S ''` in the clap-dynamic-completer switch.)
#[test]
fn zsh_qualified_prefix_completes_without_trailing_space() {
    if !precheck("zsh") {
        return;
    }
    let fx = Fixture::new("nospace");
    let typed = format!("cav cp {}", fx.slug);
    let want = format!("cav cp {}@2:", fx.slug);
    assert_eq!(fx.complete("zsh", &typed), want, "colon candidate must not gain a trailing space");
}
