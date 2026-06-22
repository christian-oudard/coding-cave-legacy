mod config;
mod constants;
mod git;
mod registry;

mod approve;
mod checkout;
mod completion;
mod cp;
mod cp_complete;
mod daemon;
mod gc;
mod gpu;
mod handler;
mod init;
mod proc_ns;
mod project;
mod pull;
mod push;
mod reject;
mod review;
mod sandbox;
mod shell;
mod status;
mod style;
mod supervisor;
mod user_env;
mod workspace;

#[cfg(test)]
mod test_util;

use std::path::{Path, PathBuf};
use std::process;

use anyhow::{bail, Result};
use clap::{Parser, Subcommand};

use crate::completion::ShellChoice;
use crate::config::{inside_cave, shared_repo_path};
use crate::constants::{HOST_SHARED_REPO_DIR};

#[derive(Parser)]
#[command(
    name = "cav",
    about = "Coding Cave: coordinate parallel AI agents on a Git project.\n\n\
             Running bare `cav` starts a new agent session.",
    disable_help_flag = false,
    disable_version_flag = true,
)]
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
        #[arg(short = 'c', long = "continue", conflicts_with = "resume_agent")]
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
        /// Bind a host device into the cave (repeatable, must be under /dev)
        #[arg(short = 'd', long = "dev", value_name = "PATH", action = clap::ArgAction::Append)]
        host_devices: Vec<String>,
        /// Bind every discrete GPU's render node into the cave.
        /// Errors when the host has no discrete GPU; use -d for iGPU passthrough.
        #[arg(long = "gpu")]
        gpu: bool,
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
    /// Reject approved commits from base branch.
    ///
    /// Rolls main back to before the rejected commit chain and deletes
    /// the approval. Other agents whose worktree branches contain the
    /// rejected commits get those commits dropped via rebase --onto.
    /// If the target agent's dev branch was torn down by approve, it is
    /// recreated at the rejected tip so the agent can continue.
    /// Pass an explicit AGENT-ID when the original committer has been
    /// gc'd to route the recreated branch to a different agent.
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
    /// Reclaim disk space from paused agents (worktree, nix overlay, branches)
    Gc {
        /// Only collect agents paused at least this long (e.g. 24h, 7d, 1w)
        #[arg(long = "older-than", default_value_t = gc::default_older_than().to_string())]
        older_than: String,
        /// Show what would be removed without removing anything
        #[arg(long = "dry-run")]
        dry_run: bool,
        /// Skip the orphan-removal confirmation prompt
        #[arg(short = 'y', long = "yes")]
        yes: bool,
    },
    /// Unregister a project: drops every owned agent's worktree, nix
    /// overlay, dev branches, and registry rows, plus the project's
    /// .coding-cave/ metadata. Source files and session logs are kept.
    #[command(name = "remove-project")]
    RemoveProject {
        slug: String,
        /// Skip confirmation
        #[arg(short = 'y', long = "yes")]
        yes: bool,
    },
    /// Reconcile cav's records after a project directory was moved or
    /// renamed on disk. Updates the registry and renames cav's worktree
    /// and session log dirs to match the new slug. Never touches the
    /// source tree. <new-path> is where the directory now lives; a bare
    /// name is taken as an in-place rename next to the old location.
    #[command(name = "rename-project")]
    RenameProject {
        old_slug: String,
        new_path: String,
        /// Skip confirmation
        #[arg(short = 'y', long = "yes")]
        yes: bool,
    },
    /// Fetch and checkout an agent branch for review
    Review {
        agent_id: Option<String>,
    },
    /// Enter a running agent's namespace (interactive or one-shot)
    Shell {
        /// Agent ID to enter (default: only running agent in the project)
        agent_id: Option<String>,
        /// Command to run non-interactively (after --)
        #[arg(last = true)]
        cmd: Vec<String>,
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
    /// Emit completion candidates for `cav cp` (used by shell scripts).
    #[command(name = "__complete-cp", hide = true)]
    CompleteCp {
        #[arg(default_value = "")]
        word: String,
    },
    /// Show version
    Version,
}

impl Cmd {
    /// Subcommands that, run inside a cave, would create or operate on a
    /// subcave. Gated by `require_subcave_opt_in()` so accidental nesting
    /// can't happen.
    fn is_subcave_op(&self) -> bool {
        matches!(
            self,
            Cmd::Init { .. }
                | Cmd::Agent { .. }
                | Cmd::Approve { .. }
                | Cmd::Reject { .. }
                | Cmd::Pull { .. }
                | Cmd::Push { .. }
                | Cmd::Status { .. }
                | Cmd::RemoveAgent { .. }
                | Cmd::Gc { .. }
                | Cmd::RemoveProject { .. }
                | Cmd::RenameProject { .. }
                | Cmd::Review { .. }
                | Cmd::Shell { .. }
                | Cmd::Checkout { .. }
        )
    }
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
        eprintln!("{}: {e}", style::bold_red("error"));
        process::exit(1);
    }
}

fn run() -> Result<()> {
    // Anchor verbose log timestamps to process start.
    let _ = config::log_prefix();

    // Rewrite argv: bare `cav` -> `cav agent`; handle -v stripping.
    let raw: Vec<String> = std::env::args().collect();
    let argv = preprocess_args(raw);

    let cli = Cli::parse_from(&argv);
    config::set_verbose(cli.verbose);

    let cmd = cli.command.unwrap_or(Cmd::Agent {
        continue_agent: false,
        resume_agent: None,
        extra_projects: vec![],
        host_ports: vec![],
        host_devices: vec![],
        gpu: false,
        comms: false,
        prompt: None,
    });
    if cmd.is_subcave_op() {
        require_subcave_opt_in();
    }
    match cmd {
        Cmd::Init { path, reinitialize } => {
            init::init(&PathBuf::from(path), reinitialize)
        }
        Cmd::Agent {
            continue_agent,
            resume_agent,
            extra_projects,
            host_ports,
            host_devices,
            gpu,
            comms,
            prompt,
        } => {
            // Expand --gpu into the effective bind list. Keep the user's
            // -d entries separately so the resume hint can echo `--gpu`
            // verbatim instead of freezing today's render-node paths.
            let host_devices_user = host_devices.clone();
            let mut effective_devices = host_devices;
            if gpu {
                for path in gpu::resolve()? {
                    effective_devices.push(path.to_string_lossy().into_owned());
                }
            }
            // May re-exec via `sg <group>` if the device's owning gid is
            // only in our supplementary set; must run before any
            // side-effecting state (registry, daemon) is touched.
            sandbox::ensure_egid_for_devices(&effective_devices)?;
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
                host_devices: effective_devices,
                host_devices_user,
                gpu,
                comms,
                prompt,
            })
        }
        Cmd::Approve { args, yes, commit } => {
            let agent_id = parse_single_agent_arg(&args, "approve")?;
            // Actions stay in the current repo (redirect: false) so they never
            // touch a project the user didn't name.
            let base = resolve_base(agent_id.as_deref(), None, false)?;
            require_init(&base)?;
            approve::approve(&base, agent_id.as_deref(), yes, commit.as_deref())
        }
        Cmd::Reject { args, yes, commit } => {
            let agent_id = parse_single_agent_arg(&args, "reject")?;
            let base = resolve_base(agent_id.as_deref(), None, false)?;
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
                    bail!("No registered projects. Run: {}", style::bold_cyan("cav init"));
                }
                status::show_all(projects)
            } else {
                // Inside a cave before cav init: show "no agents" gracefully.
                if inside_cave() {
                    if let Ok(base) = find_repo_root() {
                        if !shared_repo_path(&base).exists() {
                            let slug = config::project_slug(&base);
                            println!("{}", style::bold_white(&format!("=== {slug} ===")));
                            println!("No running agents.");
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
                    bail!("Invalid agent ID '{}'. Use a number (e.g. 3).", style::bold(&raw));
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
                        "Multiple paused agents: {}\nSpecify which: {}",
                        candidates
                            .iter()
                            .map(|a| style::bold(a))
                            .collect::<Vec<_>>()
                            .join(", "),
                        style::bold_cyan("cav remove-agent <agent-id>"),
                    ),
                }
            };
            let all_ids = registry::all_agent_ids(None)?;
            if !all_ids.contains(&aid) {
                bail!("Agent {} not found.", style::bold(&aid));
            }
            // Only operate on stopped agents — refuse before touching anything,
            // including the registry-only fallback when the project dir is gone.
            if registry::is_agent_running(&aid)? {
                bail!("Agent {} is running. Stop it first.", style::bold(&aid));
            }
            let slug = registry::agent_project(&aid);
            let path = slug
                .as_deref()
                .and_then(|s| registry::project_path(s).ok().flatten());
            if let Some(p) = path {
                let base = PathBuf::from(p);
                if !base.is_dir() {
                    registry::remove_agent(&aid)?;
                    println!(
                        "Removed agent {} (project directory missing).",
                        style::bold(&aid),
                    );
                    return Ok(());
                }
                status::remove_agent(&base, &aid, yes)
            } else {
                registry::remove_agent(&aid)?;
                println!(
                    "Removed agent {} (project directory missing).",
                    style::bold(&aid),
                );
                Ok(())
            }
        }
        Cmd::Gc { older_than, dry_run, yes } => {
            let dur = gc::parse_older_than(&older_than)?;
            gc::run(dur, dry_run, yes)
        }
        Cmd::RemoveProject { slug, yes } => project::remove(&slug, yes),
        Cmd::RenameProject { old_slug, new_path, yes } => {
            project::rename(&old_slug, &new_path, yes)
        }
        Cmd::Review { agent_id } => {
            let base = find_repo_root()?;
            require_init(&base)?;
            let aid = match agent_id {
                Some(s) => {
                    let a = parse_agent_id(&s);
                    if !config::is_agent_id(&a) {
                        bail!("Invalid agent ID '{}'. Use a number (e.g. 3).", style::bold(&s));
                    }
                    a
                }
                None => auto_pick_agent_id(&base, "review")?,
            };
            review::review(&base, &aid)
        }
        Cmd::Shell { agent_id, cmd } => {
            let base = find_repo_root()?;
            require_init(&base)?;
            let aid = match agent_id {
                Some(s) => {
                    let a = parse_agent_id(&s);
                    if !config::is_agent_id(&a) {
                        bail!("Invalid agent ID '{}'. Use a number (e.g. 3).", style::bold(&s));
                    }
                    Some(a)
                }
                None => None,
            };
            shell::enter(&base, aid.as_deref(), &cmd)
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
                bail!("Usage: {}", style::bold_cyan("cav cp <src>... <dest>"));
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
        Cmd::CompleteCp { word } => {
            for c in cp_complete::complete(&word) {
                println!("{c}");
            }
            Ok(())
        }
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
                bail!(
                    "Multiple agent IDs given: {}, {}",
                    style::bold(prev),
                    style::bold(&candidate),
                );
            }
            agent_id = Some(candidate);
        } else if looks_like_hash(arg) {
            bail!(
                "'{}' looks like a commit prefix. Did you mean: {}",
                style::yellow(arg),
                style::bold_cyan(&format!("cav {cmd} -c {arg}")),
            );
        } else {
            bail!(
                "Unknown argument '{arg}'. Usage: {}",
                style::bold_cyan(&format!("cav {cmd} [agent-id] [-c <commit>]")),
            );
        }
    }
    Ok(agent_id)
}

fn looks_like_hash(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_hexdigit())
}

fn find_repo_root() -> Result<PathBuf> {
    git::repo_root(None)
        .map(PathBuf::from)
        .map_err(|_| anyhow::anyhow!("Not a git repository. Run: cav init"))
}

/// Refuse subcave operations unless the user has explicitly opted in.
/// `cav` inside a cave can only operate on caves *below* itself; running
/// the same command on the cave you're in or on the host is impossible.
/// Require an explicit opt-in so accidental subcave creation doesn't
/// happen when the user meant to act on the parent.
///
/// Prints directly to stderr and `process::exit`s rather than returning
/// an `anyhow::Error`. This deliberately bypasses `main()`'s error
/// handler (which would prepend a red `error:` prefix) so the message
/// reads as plain advice, matching the style of `tmux`'s nesting refusal.
fn require_subcave_opt_in() {
    if inside_cave() && !config::nested_opt_in() {
        eprintln!(
            "Refusing to nest coding-cave sessions by default. \
             Enable by setting CODING_CAVE_NESTED=1."
        );
        process::exit(1);
    }
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
    match registry::project_path(&slug)? {
        // A moved checkout keeps its slug but leaves the registry pointing
        // at the old path. Re-register at the current location so commands
        // that resolve projects by slug (cav status <id>, cav agent
        // --project) stop serving the dead path.
        Some(p) if !Path::new(&p).is_dir() => {
            registry::register_project(base)?;
        }
        Some(_) => {}
        None if inside_cave() => {
            // Inside the cave the registry is isolated. Auto-register.
            registry::register_project(base)?;
        }
        None => {
            bail!(
                "Shared repo exists but project '{slug}' is not registered.\n\
                 If this project was renamed from another directory, run:\n\
                   cav remove-project <old-slug>   # remove stale registry entry\n\
                   cav init                        # re-register under new name"
            );
        }
    }
    config::check_workspace_consistent(base)?;
    Ok(())
}

/// Resolve a project base from explicit agent_id / project_slug args.
/// Returns `Ok(None)` when neither is given (caller falls back to CWD).
/// Bails when the args disagree (slug given alongside an agent that lives
/// elsewhere) or when the named agent isn't registered.
/// True when `agent_id` can reach the project rooted at `base` — its home
/// project or one of the extra projects it was granted.
fn agent_visible_in(agent_id: &str, base: &Path) -> bool {
    let slug = config::project_slug(base);
    registry::agent_all_slugs(agent_id)
        .map(|slugs| slugs.contains(&slug))
        .unwrap_or(false)
}

/// Resolve a registered project slug to its on-disk base directory.
fn project_base(slug: &str) -> Result<PathBuf> {
    let p = registry::project_path(slug)?
        .ok_or_else(|| anyhow::anyhow!("No project '{slug}'. Check spelling or run: cav init"))?;
    let b = PathBuf::from(p);
    if !b.is_dir() {
        bail!("Project '{slug}' path missing: {}", b.display());
    }
    Ok(b)
}

/// Resolve which project directory a command acts on, given optional project
/// and agent args, anchored to where the user is standing. The shared rule
/// behind `cav status`, `cav approve`, and `cav reject`:
///
/// - An explicit project arg wins, validated against the agent's reachable
///   projects (home or guest) when an agent is also named.
/// - Otherwise the current repo wins when it can serve the request: no agent
///   was named, or the named agent has presence here.
/// - When an agent was named but isn't present in the current repo,
///   `redirect` decides. Queries (status) jump to the agent's home project so
///   they can show it from anywhere. Actions (approve/reject) stay in the
///   current repo so they never touch a project the user didn't name.
/// - Outside any repo, both fall back to the agent's home project.
fn resolve_base(
    agent_id: Option<&str>,
    project_slug: Option<&str>,
    redirect: bool,
) -> Result<PathBuf> {
    if let Some(slug) = project_slug {
        if let Some(aid) = agent_id {
            let slugs = registry::agent_all_slugs(aid)?;
            if slugs.is_empty() {
                bail!("No agent {aid} in registry.");
            }
            if !slugs.iter().any(|s| s == slug) {
                bail!(
                    "Agent {aid} has no access to project '{slug}' (reaches: {}).",
                    slugs.join(", "),
                );
            }
        }
        return project_base(slug);
    }
    if let Ok(root) = git::repo_root(None) {
        let base = PathBuf::from(root);
        let serves_here = agent_id.map_or(true, |aid| agent_visible_in(aid, &base));
        if serves_here || !redirect {
            return Ok(base);
        }
    }
    if let Some(aid) = agent_id {
        let slug = registry::agent_project(aid)
            .ok_or_else(|| anyhow::anyhow!("No agent {aid} in registry."))?;
        return project_base(&slug);
    }
    find_repo_root()
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

    // Status is a query: redirect to the agent's home project when it isn't
    // present in the current repo, so `cav status <id>` works from anywhere.
    // The current project still anchors the per-project ordering downstream.
    let base = resolve_base(agent_id.as_deref(), project_slug.as_deref(), true)?;

    require_init(&base)?;

    if infer_agent && agent_id.is_none() {
        agent_id = git::current_agent_id(Some(&base));
    }

    Ok((base, agent_id))
}

/// Pick an agent ID for the current project when none was given.
/// Tries the current dev branch first, then falls back to the only
/// active (non-archived) agent registered for this project. Bails
/// with a helpful message when there are zero or multiple candidates.
fn auto_pick_agent_id(base: &Path, cmd: &str) -> Result<String> {
    if let Some(aid) = git::current_agent_id(Some(base)) {
        return Ok(aid);
    }
    let slug = config::project_slug(base);
    let ids: Vec<String> = registry::all_agent_ids(Some(&slug))?
        .into_iter()
        .filter(|aid| {
            aid.parse::<u32>()
                .map(|n| n < workspace::ARCHIVE_INT_BASE)
                .unwrap_or(true)
        })
        .collect();
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
    let secs = dt
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0) as libc::time_t;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    if unsafe { libc::localtime_r(&secs, &mut tm) }.is_null() {
        let (y, mo, d, h, min) = unix_to_ymdhm(secs as u64);
        return format!("{y:04}-{mo:02}-{d:02} {h:02}:{min:02}");
    }
    format!(
        "{:04}-{:02}-{:02} {:02}:{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
    )
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

#[cfg(test)]
mod resolve_base_tests {
    use super::*;
    use crate::test_util::DataDirGuard;
    use std::fs;

    fn register_project_with_agent(data: &Path, slug: &str) -> (PathBuf, String) {
        let base = data.join(slug);
        fs::create_dir_all(&base).unwrap();
        registry::register_project(&base).unwrap();
        let aid = registry::acquire_agent_id(&base).unwrap();
        (base, aid)
    }

    #[test]
    fn agent_only_redirects_to_home_project() {
        // `cav status <id>` from a repo the agent can't reach lands on the
        // agent's home project (redirect: true).
        let g = DataDirGuard::new("rb-redirect");
        let (proj_b, aid) = register_project_with_agent(&g.path, "proj-b");
        // Also register an unrelated project — the lookup must pick proj-b.
        let _ = register_project_with_agent(&g.path, "proj-a");

        let base = resolve_base(Some(&aid), None, true).unwrap();
        assert_eq!(base.canonicalize().unwrap(), proj_b.canonicalize().unwrap());
    }

    #[test]
    fn named_project_unreachable_by_agent_is_rejected() {
        let g = DataDirGuard::new("rb-mismatch");
        let (_, aid) = register_project_with_agent(&g.path, "proj-b");
        let _ = register_project_with_agent(&g.path, "proj-a");

        let err = resolve_base(Some(&aid), Some("proj-a"), true).expect_err("must error");
        let msg = err.to_string();
        assert!(
            msg.contains("proj-b") && msg.contains("proj-a"),
            "error should name both the agent's project and the bad one, got: {msg}",
        );
    }

    #[test]
    fn named_guest_project_is_accepted() {
        // A project the agent guests on (extra_projects) must resolve, not
        // bail. This was the bug: validation only checked the home project.
        let g = DataDirGuard::new("rb-guest");
        let (_, aid) = register_project_with_agent(&g.path, "proj-home");
        let guest = g.path.join("proj-guest");
        fs::create_dir_all(&guest).unwrap();
        registry::register_project(&guest).unwrap();
        registry::save_agent_extras(&aid, &["proj-guest".to_string()]).unwrap();

        let base = resolve_base(Some(&aid), Some("proj-guest"), true).unwrap();
        assert_eq!(base.canonicalize().unwrap(), guest.canonicalize().unwrap());
    }

    #[test]
    fn unknown_agent_is_rejected() {
        let g = DataDirGuard::new("rb-no-agent");
        let _ = register_project_with_agent(&g.path, "proj-b");
        let err = resolve_base(Some("999"), Some("proj-b"), true).expect_err("must error");
        assert!(err.to_string().contains("999"), "got: {err}");
    }

    #[test]
    fn agent_visible_in_covers_home_and_guest_projects() {
        let g = DataDirGuard::new("tb-visible");
        let (home, aid) = register_project_with_agent(&g.path, "proj-home");
        let guest = g.path.join("proj-guest");
        fs::create_dir_all(&guest).unwrap();
        registry::register_project(&guest).unwrap();
        registry::save_agent_extras(&aid, &["proj-guest".to_string()]).unwrap();

        assert!(agent_visible_in(&aid, &home), "home project must be visible");
        assert!(agent_visible_in(&aid, &guest), "guest project must be visible");

        let other = g.path.join("proj-other");
        fs::create_dir_all(&other).unwrap();
        assert!(
            !agent_visible_in(&aid, &other),
            "a project the agent can't reach must not be visible",
        );
    }
}
