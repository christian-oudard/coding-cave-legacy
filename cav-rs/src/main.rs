mod config;
mod constants;
mod git;
mod registry;

mod approve;
mod checkout;
mod completion;
mod cp;
mod daemon;
mod handler;
mod init;
mod pull;
mod push;
mod reject;
mod review;
mod sandbox;
mod status;
mod supervisor;
mod user_env;
mod workspace;

use std::path::{Path, PathBuf};
use std::process;

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};

use crate::completion::ShellChoice;
use crate::config::{inside_cave, parse_dev_branch, shared_repo_path};
use crate::constants::{HOST_SHARED_REPO_DIR};

#[derive(Parser)]
#[command(name = "cav", disable_help_flag = false, disable_version_flag = true)]
struct Cli {
    /// Enable verbose output (-v info, -vv debug)
    #[arg(short = 'v', action = clap::ArgAction::Count, global = true)]
    verbose: u8,
    #[command(subcommand)]
    command: Option<Cmd>,
}

#[derive(Subcommand)]
enum Cmd {
    /// Initialize Coding Cave in a project
    Init {
        #[arg(default_value = ".")]
        path: String,
        /// Reconfigure an already-initialized project
        #[arg(long)]
        reinitialize: bool,
    },
    /// Launch an agent in a sandbox
    Agent {
        /// Continue the most recently paused agent
        #[arg(short = 'c', long = "continue")]
        continue_agent: bool,
        /// Resume a specific agent (e.g. @3, coding-cave@3)
        #[arg(short = 'r', long = "resume", value_name = "AGENT")]
        resume_agent: Option<String>,
        /// Additional project to mount (repeatable)
        #[arg(short = 'p', long = "project", value_name = "PATH", action = clap::ArgAction::Append)]
        extra_projects: Vec<String>,
        /// Expose a host TCP port inside the cave (repeatable)
        #[arg(long = "port", value_name = "PORT", action = clap::ArgAction::Append)]
        host_ports: Vec<u16>,
        /// Enable inter-agent communication (send_message tool + channel events)
        #[arg(long = "comms")]
        comms: bool,
        /// Non-interactive mode: execute prompt, print response, exit
        #[arg(long)]
        prompt: Option<String>,
    },
    /// Approve agent work into base branch
    Approve {
        #[arg(value_name = "AGENT-ID")]
        args: Vec<String>,
        /// Skip confirmation
        #[arg(short = 'y', long = "yes")]
        yes: bool,
        /// Approve only up to this commit
        #[arg(short = 'c', long = "commit", value_name = "SHA")]
        commit: Option<String>,
    },
    /// Reject approved commits from base branch
    Reject {
        #[arg(value_name = "AGENT-ID")]
        args: Vec<String>,
        /// Skip confirmation
        #[arg(short = 'y', long = "yes")]
        yes: bool,
        /// Reject from this commit through the tip
        #[arg(short = 'c', long = "commit", value_name = "SHA")]
        commit: Option<String>,
    },
    /// Pull dev branch from shared remote
    Pull {
        #[arg(long)]
        force: bool,
    },
    /// Push current branch to shared remote
    Push {
        #[arg(short = 'f', long)]
        force: bool,
    },
    /// Show agent and dev branch status
    Status {
        #[arg(value_name = "[PROJECT] [AGENT-ID]")]
        args: Vec<String>,
        /// Show status for all registered projects
        #[arg(short = 'a', long = "all")]
        all_projects: bool,
    },
    /// Remove a paused agent and its branches
    #[command(name = "remove-agent")]
    RemoveAgent {
        agent_id: Option<String>,
        /// Skip confirmation
        #[arg(short = 'y', long = "yes")]
        yes: bool,
    },
    /// Unregister a project from the cave
    #[command(name = "remove-project")]
    RemoveProject {
        slug: String,
    },
    /// Fetch and checkout an agent branch for review
    Review {
        agent_id: Option<String>,
    },
    /// Retarget an agent to deliver to a different branch
    Checkout {
        branch: String,
        agent_id: Option<String>,
    },
    /// Copy files to/from a cave agent worktree
    Cp {
        #[arg(required = true)]
        paths: Vec<String>,
        #[arg(short = 'R', short_alias = 'r', long = "recursive")]
        recursive: bool,
        #[arg(short = 'f', long = "force")]
        force: bool,
    },
    /// Manage the cave environment
    Env {
        #[command(subcommand)]
        cmd: EnvCmd,
    },
    /// Print shell completion script
    Completion {
        #[arg(value_enum)]
        shell: ShellChoice,
    },
    /// Show version
    Version,
}

#[derive(Subcommand)]
enum EnvCmd {
    /// Rebuild the cave environment from cave.nix
    Build {
        /// Rebuild even if environment is unchanged
        #[arg(long)]
        rebuild: bool,
    },
    /// Update flake lock file (does not rebuild)
    Update {
        input_name: Option<String>,
    },
    /// Show cave environment flake input versions
    Metadata,
}


fn main() {
    if let Err(e) = run() {
        eprintln!("{e}");
        process::exit(1);
    }
}

fn run() -> Result<()> {
    // Rewrite argv: bare `cav` -> `cav agent`; handle -v stripping.
    let raw: Vec<String> = std::env::args().collect();
    let argv = preprocess_args(raw);

    let cli = Cli::parse_from(&argv);
    config::set_verbose(cli.verbose);

    match cli.command.unwrap_or(Cmd::Agent {
        continue_agent: false,
        resume_agent: None,
        extra_projects: vec![],
        host_ports: vec![],
        comms: false,
        prompt: None,
    }) {
        Cmd::Init { path, reinitialize } => {
            init::init(&PathBuf::from(path), reinitialize)
        }
        Cmd::Agent {
            continue_agent,
            resume_agent,
            extra_projects,
            host_ports,
            comms,
            prompt,
        } => {
            let base = find_repo_root()?;
            require_init(&base)?;
            let extras = extra_projects
                .iter()
                .map(|p| {
                    resolve_project(p).map(|pb| pb.to_string_lossy().into_owned())
                })
                .collect::<Result<Vec<_>>>()?;
            sandbox::run_agent(sandbox::RunAgentOpts {
                base,
                continue_agent,
                resume_agent,
                extra_projects: extras,
                host_ports,
                comms,
                prompt,
            })
        }
        Cmd::Approve { args, yes, commit } => {
            let agent_id = parse_single_agent_arg(&args, "approve")?;
            let base = approve_reject_base(agent_id.as_deref())?;
            require_init(&base)?;
            approve::approve(&base, agent_id.as_deref(), yes, commit.as_deref())
        }
        Cmd::Reject { args, yes, commit } => {
            let agent_id = parse_single_agent_arg(&args, "reject")?;
            let base = approve_reject_base(agent_id.as_deref())?;
            require_init(&base)?;
            reject::reject(&base, agent_id.as_deref(), yes, commit.as_deref())
        }
        Cmd::Pull { force } => {
            let base = find_repo_root()?;
            require_init(&base)?;
            pull::pull(&base, force)
        }
        Cmd::Push { force } => {
            let base = find_repo_root()?;
            require_init(&base)?;
            push::push(&base, force)
        }
        Cmd::Status { args, all_projects } => {
            let all = if args.is_empty() && !all_projects {
                // Fall back to all-projects if not in a git repo
                let in_repo = git::repo_root(None).is_ok();
                !in_repo
            } else {
                all_projects
            };
            if all {
                let projects = registry::all_projects()?;
                if projects.is_empty() {
                    bail!("No registered projects. Run: cav init");
                }
                status::show_all(projects)
            } else {
                // Inside a cave before cav init: show "no agents" gracefully.
                if inside_cave() {
                    if let Ok(base) = find_repo_root() {
                        if !shared_repo_path(&base).exists() {
                            let slug = config::project_slug(&base);
                            println!("No running agents in {slug}.");
                            return Ok(());
                        }
                    }
                }
                let (base, agent_id) = resolve_target(&args, true)?;
                status::show(&base, agent_id.as_deref())
            }
        }
        Cmd::RemoveAgent { agent_id, yes } => {
            let aid = if let Some(raw) = agent_id {
                let a = parse_agent_id(&raw);
                if !config::is_agent_id(&a) {
                    bail!("Invalid agent ID '{raw}'. Use a number (e.g. 3).");
                }
                a
            } else {
                let base = find_repo_root().ok();
                let slug = base.as_deref().map(config::project_slug);
                let candidates: Vec<String> = registry::all_agent_ids(slug.as_deref())?
                    .into_iter()
                    .filter(|a| !registry::is_agent_running(a).unwrap_or(false))
                    .collect();
                match candidates.len() {
                    0 => bail!("No paused agents to remove."),
                    1 => candidates.into_iter().next().unwrap(),
                    _ => bail!(
                        "Multiple paused agents: {}\nSpecify which: cav remove-agent <agent-id>",
                        candidates.join(", ")
                    ),
                }
            };
            let all_ids = registry::all_agent_ids(None)?;
            if !all_ids.contains(&aid) {
                bail!("Agent {aid} not found.");
            }
            let slug = registry::agent_project(&aid);
            let path = slug
                .as_deref()
                .and_then(|s| registry::project_path(s).ok().flatten());
            if let Some(p) = path {
                let base = PathBuf::from(p);
                if !base.is_dir() {
                    registry::remove_agent(&aid)?;
                    println!("Removed agent {aid} (project directory missing).");
                    return Ok(());
                }
                if registry::is_agent_running(&aid)? {
                    let name = registry::display_name(&base, &aid);
                    bail!("{name} is running. Stop it first.");
                }
                status::remove_agent(&base, &aid, yes)
            } else {
                registry::remove_agent(&aid)?;
                println!("Removed agent {aid} (project directory missing).");
                Ok(())
            }
        }
        Cmd::RemoveProject { slug } => {
            let projects: std::collections::HashMap<_, _> =
                registry::all_projects()?.into_iter().collect();
            if !projects.contains_key(&slug) {
                bail!("No project \"{slug}\" in registry.");
            }
            registry::remove_project(&slug)?;
            println!("Removed project \"{slug}\".");
            Ok(())
        }
        Cmd::Review { agent_id } => {
            let base = find_repo_root()?;
            require_init(&base)?;
            let aid = match agent_id {
                Some(s) => {
                    let a = parse_agent_id(&s);
                    if !config::is_agent_id(&a) {
                        bail!("Invalid agent ID '{s}'. Use a number (e.g. 3).");
                    }
                    a
                }
                None => auto_pick_agent_id(&base, "review")?,
            };
            review::review(&base, &aid)
        }
        Cmd::Checkout { branch, agent_id } => {
            let base = find_repo_root()?;
            if !inside_cave() {
                require_init(&base)?;
            }
            checkout::checkout(&base, &branch, agent_id.as_deref())
        }
        Cmd::Cp { paths, recursive, force } => {
            if paths.len() < 2 {
                bail!("Usage: cav cp <src>... <dest>");
            }
            let srcs = paths[..paths.len() - 1].to_vec();
            let dest = paths.last().unwrap().clone();
            cp::cp(srcs, dest, recursive, force)
        }
        Cmd::Env { cmd } => match cmd {
            EnvCmd::Build { rebuild } => user_env::build(rebuild),
            EnvCmd::Update { input_name } => user_env::update(input_name.as_deref()),
            EnvCmd::Metadata => user_env::metadata(),
        },
        Cmd::Completion { shell } => completion::print_completion(shell),
        Cmd::Version => {
            let vd = constants::version_date();
            if vd.chars().all(|c| c.is_ascii_digit()) {
                use std::time::{Duration, UNIX_EPOCH};
                let ts: u64 = vd.parse().unwrap_or(0);
                let dt = UNIX_EPOCH + Duration::from_secs(ts);
                let dt_str = format_datetime(dt);
                println!("cav {} ({})", constants::version(), dt_str);
            } else {
                println!("cav {} ({})", constants::version(), vd);
            }
            Ok(())
        }
    }
}

fn preprocess_args(raw: Vec<String>) -> Vec<String> {
    // raw[0] is the binary name. Rewrite bare `cav <flags>` to
    // `cav agent <flags>` so shortcuts like `cav -p ../proj` work.
    // Subcommand and top-level flag names come from clap reflection so
    // this stays correct when the `Cli` definition changes.
    use clap::CommandFactory;

    let mut argv = vec![raw[0].clone()];
    let rest = &raw[1..];
    let cmd = Cli::command();

    let subcommands: Vec<String> = cmd
        .get_subcommands()
        .map(|c| c.get_name().to_string())
        .collect();
    let mut top_flags: Vec<String> = Vec::new();
    for a in cmd.get_arguments() {
        if let Some(c) = a.get_short() {
            top_flags.push(format!("-{c}"));
        }
        if let Some(s) = a.get_long() {
            top_flags.push(format!("--{s}"));
        }
    }
    // clap auto-adds help; make sure it's recognized even if reflection
    // doesn't surface it in a future version.
    for h in ["-h", "--help"] {
        if !top_flags.iter().any(|f| f == h) {
            top_flags.push(h.to_string());
        }
    }

    // `-v` has ArgAction::Count, so it can stack as `-vv`, `-vvv`, etc.
    // Consume any leading stacked verbose tokens before the subcommand check.
    let is_verbose_token = |a: &str| {
        a == "--verbose"
            || (a.starts_with('-')
                && !a.starts_with("--")
                && a.len() > 1
                && a[1..].chars().all(|c| c == 'v'))
    };

    let mut idx = 0;
    while idx < rest.len() && is_verbose_token(&rest[idx]) {
        argv.push(rest[idx].clone());
        idx += 1;
    }

    if let Some(next) = rest.get(idx) {
        let s = next.as_str();
        let is_sub = subcommands.iter().any(|n| n == s);
        let is_top = top_flags.iter().any(|n| n == s);
        if !is_sub && !is_top {
            argv.push("agent".to_string());
        }
    }

    argv.extend(rest[idx..].iter().cloned());
    argv
}

fn parse_agent_id(s: &str) -> String {
    s.trim_start_matches('@').split('@').next_back().unwrap_or(s).to_string()
}

fn parse_single_agent_arg(args: &[String], cmd: &str) -> Result<Option<String>> {
    let mut agent_id: Option<String> = None;
    for arg in args {
        let candidate = parse_agent_id(arg);
        if config::is_agent_id(&candidate) {
            if let Some(prev) = &agent_id {
                bail!("Multiple agent IDs given: {prev}, {candidate}");
            }
            agent_id = Some(candidate);
        } else if looks_like_hash(arg) {
            bail!("'{arg}' looks like a commit prefix. Did you mean: cav {cmd} -c {arg}");
        } else {
            bail!(
                "Unknown argument '{arg}'. Usage: cav {cmd} [agent-id] [-c <commit>]"
            );
        }
    }
    Ok(agent_id)
}

fn looks_like_hash(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_hexdigit())
}

fn approve_reject_base(agent_id: Option<&str>) -> Result<PathBuf> {
    if let Some(aid) = agent_id {
        if let Some(slug) = registry::agent_project(aid) {
            if let Some(p) = registry::project_path(&slug)? {
                return Ok(PathBuf::from(p));
            }
        }
    }
    find_repo_root()
}

fn find_repo_root() -> Result<PathBuf> {
    git::repo_root(None)
        .map(PathBuf::from)
        .map_err(|_| anyhow::anyhow!("Not a git repository. Run: cav init"))
}

fn require_init(base: &Path) -> Result<()> {
    if !shared_repo_path(base).exists() {
        bail!("Coding Cave not initialized. Run: cav init");
    }
    if inside_cave() {
        let host_shared = config::cav_dir(base).join(HOST_SHARED_REPO_DIR);
        if !host_shared.exists() {
            bail!("Coding Cave not initialized. Run: cav init");
        }
    }
    let slug = config::project_slug(base);
    if registry::project_path(&slug)?.is_none() {
        if inside_cave() {
            // Inside the cave, the registry is isolated — auto-register.
            registry::register_project(base)?;
        } else {
            bail!("Shared repo exists but project not registered. Run: cav init");
        }
    }
    config::check_workspace_consistent(base)?;
    Ok(())
}

pub fn resolve_target(
    args: &[String],
    infer_agent: bool,
) -> Result<(PathBuf, Option<String>)> {
    let mut agent_id: Option<String> = None;
    let mut project_slug: Option<String> = None;

    for arg in args {
        let candidate = parse_agent_id(arg);
        if config::is_agent_id(&candidate) {
            if let Some(prev) = &agent_id {
                bail!("Multiple agent IDs given: {prev}, {candidate}");
            }
            agent_id = Some(candidate);
        } else {
            if let Some(prev) = &project_slug {
                bail!("Multiple projects given: {prev}, {arg}");
            }
            project_slug = Some(arg.clone());
        }
    }

    let base = if let Some(slug) = project_slug {
        let p = registry::project_path(&slug)?
            .ok_or_else(|| anyhow::anyhow!("No project '{slug}'. Check spelling or run: cav init"))?;
        let b = PathBuf::from(p);
        if !b.is_dir() {
            bail!("Project '{slug}' path missing: {}", b.display());
        }
        b
    } else {
        find_repo_root()?
    };

    require_init(&base)?;

    if infer_agent && agent_id.is_none() {
        if let Ok(branch) = git::current_branch(Some(&base)) {
            if let Some((_, aid)) = parse_dev_branch(&branch) {
                agent_id = Some(aid);
            }
        }
    }

    Ok((base, agent_id))
}

/// Pick an agent ID for the current project when none was given.
/// Tries the current dev branch first, then falls back to the only
/// agent registered for this project. Bails with a helpful message
/// when there are zero or multiple candidates.
fn auto_pick_agent_id(base: &Path, cmd: &str) -> Result<String> {
    if let Ok(branch) = git::current_branch(Some(base)) {
        if let Some((_, aid)) = parse_dev_branch(&branch) {
            return Ok(aid);
        }
    }
    let slug = config::project_slug(base);
    let ids = registry::all_agent_ids(Some(&slug))?;
    match ids.len() {
        1 => Ok(ids.into_iter().next().unwrap()),
        0 => bail!("No agents in {slug}."),
        _ => bail!(
            "Multiple agents in {slug}: {}\nSpecify which: cav {cmd} <agent-id>",
            ids.join(", ")
        ),
    }
}

fn resolve_project(path: &str) -> Result<PathBuf> {
    if let Some(p) = registry::project_path(path)? {
        return Ok(PathBuf::from(p));
    }
    let p = PathBuf::from(path)
        .canonicalize()
        .map_err(|e| anyhow::anyhow!("Cannot resolve project '{path}': {e}"))?;
    if !p.is_dir() {
        bail!("No project named '{path}'");
    }
    if !p.join(".git").exists() {
        bail!("Not a git repository: {}", p.display());
    }
    if !shared_repo_path(&p).exists() {
        bail!(
            "Coding Cave not initialized in {}\nRun: cav init {}",
            p.display(),
            p.display()
        );
    }
    let slug = config::project_slug(&p);
    if registry::project_path(&slug)?.is_none() {
        bail!("Project not registered: {}\nRun: cav init {}", p.display(), p.display());
    }
    Ok(p)
}

fn format_datetime(dt: std::time::SystemTime) -> String {
    // Simple UTC formatting without chrono dependency
    let secs = dt
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let (y, mo, d, h, min) = unix_to_ymdhm(secs);
    format!("{y:04}-{mo:02}-{d:02} {h:02}:{min:02}")
}

fn unix_to_ymdhm(secs: u64) -> (u64, u64, u64, u64, u64) {
    let min = (secs / 60) % 60;
    let h = (secs / 3600) % 24;
    let mut days = secs / 86400;
    let mut y = 1970u64;
    loop {
        let dy = if y.is_multiple_of(4) && (!y.is_multiple_of(100) || y.is_multiple_of(400)) { 366 } else { 365 };
        if days < dy { break; }
        days -= dy;
        y += 1;
    }
    let leap = y.is_multiple_of(4) && (!y.is_multiple_of(100) || y.is_multiple_of(400));
    let mdays = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut mo = 1u64;
    for dm in &mdays {
        if days < *dm { break; }
        days -= dm;
        mo += 1;
    }
    (y, mo, days + 1, h, min)
}
