use anyhow::Result;
use clap::CommandFactory;
use clap_complete::Shell;

#[derive(clap::ValueEnum, Clone)]
pub enum ShellChoice {
    Bash,
    Zsh,
    Fish,
}

pub fn print_completion(shell: ShellChoice) -> Result<()> {
    let sh = match shell {
        ShellChoice::Bash => Shell::Bash,
        ShellChoice::Zsh => Shell::Zsh,
        ShellChoice::Fish => Shell::Fish,
    };
    let mut cmd = crate::Cli::command();
    let mut buf: Vec<u8> = Vec::new();
    clap_complete::generate(sh, &mut cmd, "cav", &mut buf);
    let script = String::from_utf8(buf)?;
    let script = inject_cp_completion(sh, &script)?;
    print!("{script}");
    Ok(())
}

/// Replace `cav`'s static completion for two dynamic arguments with
/// calls to runtime helpers:
///   - `cav cp` paths -> `__complete-cp` (registered slugs, agent IDs,
///     worktree contents)
///   - `cav -p`/`--project` -> `__complete-projects` (registered slugs),
///     merged with the shell's own directory completion so paths still
///     work.
///
/// Fails loudly when an expected anchor is missing: that means
/// clap_complete changed its template and our string-based patching
/// would silently no-op, leaving users with plain file completion.
fn inject_cp_completion(shell: Shell, script: &str) -> Result<String> {
    match shell {
        Shell::Zsh => inject_zsh(script),
        Shell::Bash => inject_bash(script),
        Shell::Fish => inject_fish(script),
        _ => Ok(script.to_string()),
    }
}

fn inject_zsh(script: &str) -> Result<String> {
    let needle = "'*::paths:_default' \\";
    let patched = replace_once(script, needle, "'*::paths:_cav_cp_paths' \\", "zsh")?;
    // `-p`/`--project`: swap file completion for the slug-aware helper.
    let patched = replace_once(
        &patched,
        "'*-p+[Additional project to mount (repeatable)]:PATH:_default' \\",
        "'*-p+[Additional project to mount (repeatable)]:PATH:_cav_projects' \\",
        "zsh",
    )?;
    let patched = replace_once(
        &patched,
        "'*--project=[Additional project to mount (repeatable)]:PATH:_default' \\",
        "'*--project=[Additional project to mount (repeatable)]:PATH:_cav_projects' \\",
        "zsh",
    )?;
    // clap emits `_describe` entries for the hidden `__complete-*`
    // subcommands even though they're marked `hide = true`; drop them so
    // they don't appear in `cav <TAB>` menus.
    let patched = strip_lines_containing(&patched, "'__complete-cp:");
    let patched = strip_lines_containing(&patched, "'__complete-projects:");
    Ok(format!("{patched}\n{ZSH_HELPER}\n{ZSH_PROJ_HELPER}"))
}

const ZSH_PROJ_HELPER: &str = r#"
_cav_projects() {
    local -a slugs
    slugs=(${(f)"$(cav __complete-projects -- "${(Q)PREFIX}" 2>/dev/null)"})
    compadd -- "${slugs[@]}"
    # Projects can also be given as a path; keep directory completion.
    _files -/
}
"#;

const ZSH_HELPER: &str = r#"
_cav_cp_paths() {
    # Dequote `$PREFIX`. zsh backslash-escapes the `~` when inserting a
    # tilde candidate, so subsequent tabs arrive as `\~/...`; the helper
    # only understands the unquoted form.
    local cur="${(Q)PREFIX}"

    # When the word starts with `~/` or `~user/`, hand that segment to
    # zsh as IPREFIX (an already-accepted prefix that compadd leaves
    # alone). Without this, compadd re-quotes the tilde on every
    # insertion and `cp` ends up with `\~/...` in the buffer instead
    # of the plain `~/...` that `_files` produces.
    local tilde=""
    if [[ "$cur" == "~"*/* ]]; then
        tilde="${cur%%/*}/"
        IPREFIX+="$tilde"
        PREFIX="${cur#$tilde}"
        cur="$PREFIX"
    fi
    local query="$tilde$cur"

    local -a candidates
    candidates=(${(f)"$(cav __complete-cp -- "$query" 2>/dev/null)"})
    local cand
    for cand in "${candidates[@]}"; do
        local -a addargs
        addargs=()
        # Strip the same tilde segment from the candidate so compadd
        # only handles the part after IPREFIX.
        local body="${cand#$tilde}"
        if [[ "$body" == */ || "$body" == *: ]]; then
            addargs+=(-S '')
        fi
        if [[ "$body" != "$cur"* ]]; then
            # Candidate replaces a longer-than-PREFIX user input (e.g.
            # `cave:` -> `cave@7:`); -U skips compadd's match check.
            addargs+=(-U)
        fi
        compadd "${addargs[@]}" -- "$body"
    done
}
"#;

fn inject_bash(script: &str) -> Result<String> {
    // Replace the static cp branch with one that calls the helper.
    // The cp arm contains a nested `case "$prev" in ... esac`, so the
    // first `;;` we see closes the inner arm, not the outer one. Walk
    // forward to the next outer-level arm (`        cav__subcmd_*)`)
    // and trim back to the preceding `;;`.
    let needle = "        cav__subcmd__cp)\n";
    let start = script.find(needle).ok_or_else(|| anchor_err("bash", needle))?;
    let after_start = start + needle.len();
    let next_marker = "\n        cav__subcmd__";
    let next_arm_rel = script[after_start..]
        .find(next_marker)
        .ok_or_else(|| anchor_err("bash", next_marker))?;
    let next_arm = after_start + next_arm_rel + 1;
    let mut out = String::new();
    out.push_str(&script[..start]);
    out.push_str(BASH_CP_ARM);
    out.push_str(&script[next_arm..]);
    // `-p`/`--project`: swap file completion for the slug-aware helper.
    out = replace_once(&out, BASH_PROJECT_ARM, BASH_PROJECT_ARM_NEW, "bash")?;
    out = replace_once(&out, BASH_P_ARM, BASH_P_ARM_NEW, "bash")?;
    out.push_str(BASH_PROJ_HELPER);
    // Drop the hidden `__complete-*` subcommands from the user-visible
    // opts strings so they don't surface in `cav <TAB>` menus. Only
    // rewrite lines that contain `opts=` so we don't mangle the literal
    // `cav __complete-*` helper invocations.
    Ok(out
        .lines()
        .map(|l| {
            if l.contains("opts=") {
                l.replace(" __complete-cp", "").replace(" __complete-projects", "")
            } else {
                l.to_string()
            }
        })
        .collect::<Vec<_>>()
        .join("\n")
        + "\n")
}

const BASH_PROJECT_ARM: &str = "                --project)\n                    COMPREPLY=($(compgen -f \"${cur}\"))\n                    return 0\n                    ;;";
const BASH_PROJECT_ARM_NEW: &str = "                --project)\n                    _cav_complete_projects \"${cur}\"\n                    return 0\n                    ;;";
const BASH_P_ARM: &str = "                -p)\n                    COMPREPLY=($(compgen -f \"${cur}\"))\n                    return 0\n                    ;;";
const BASH_P_ARM_NEW: &str = "                -p)\n                    _cav_complete_projects \"${cur}\"\n                    return 0\n                    ;;";

const BASH_PROJ_HELPER: &str = r#"
_cav_complete_projects() {
    local cur="$1"
    local _cav_slugs
    _cav_slugs="$(cav __complete-projects -- "${cur}" 2>/dev/null)"
    # Registered slugs, plus directories so plain paths still complete.
    COMPREPLY=( $(compgen -W "${_cav_slugs}" -- "${cur}") $(compgen -d -- "${cur}") )
}
"#;

const BASH_CP_ARM: &str = r#"        cav__subcmd__cp)
            if [[ "${cur}" == -* ]]; then
                COMPREPLY=( $(compgen -W "-R --recursive -f --force -h --help" -- "${cur}") )
            else
                # Bash's default COMP_WORDBREAKS includes `:` and `@`,
                # so `slug:path` arrives split across multiple
                # COMP_WORDS. Reconstruct the full word from COMP_LINE
                # and trim the pre-cur prefix from each candidate so
                # bash inserts the right text at the cursor.
                local _cav_line="${COMP_LINE:0:$COMP_POINT}"
                local _cav_word="${_cav_line##* }"
                local _cav_pfx="${_cav_word%$cur}"
                local IFS=$'\n'
                local -a _cav_raw
                _cav_raw=( $(cav __complete-cp -- "${_cav_word}" 2>/dev/null) )
                COMPREPLY=()
                local _cav_c
                for _cav_c in "${_cav_raw[@]}"; do
                    # Bash can only replace cur, not back up past it.
                    # When the candidate doesn't share the pre-cur
                    # prefix the helper emitted (e.g. user typed the
                    # `cave` shorthand and the candidate uses the full
                    # slug name), skip it rather than inserting in the
                    # wrong place. Type the slug to get the rewrite.
                    if [[ -n "$_cav_pfx" && "$_cav_c" != "$_cav_pfx"* ]]; then
                        continue
                    fi
                    COMPREPLY+=("${_cav_c#$_cav_pfx}")
                done
                if [[ "${COMPREPLY[*]}" == */ || "${COMPREPLY[*]}" == *: ]]; then
                    compopt -o nospace 2>/dev/null
                fi
            fi
            return 0
            ;;
"#;

fn inject_fish(script: &str) -> Result<String> {
    // Drop `complete` lines that surface `__complete-cp` to the user.
    // Lines that *use* the helper (the `__cav_cp_complete` function we
    // append below) are not present in the generated script, so this
    // simple filter is safe. The remaining mentions in
    // `__fish_seen_subcommand_from ...` predicates aren't user-facing
    // suggestions, so they can stay.
    let needle = "-a \"__complete-cp\"";
    if !script.contains(needle) {
        return Err(anchor_err("fish", needle));
    }
    let stripped = strip_lines_containing(script, needle);
    let stripped = strip_lines_containing(&stripped, "-a \"__complete-projects\"");
    // Append `complete` rules that run after the generated ones. Fish
    // honors the last matching rule, so this overrides the default
    // file completion for `cav cp`. The `-p`/`--project` rule adds slug
    // candidates on top of the file completion clap already emits.
    Ok(format!(
        "{stripped}\n\
         function __cav_cp_complete\n    \
             cav __complete-cp -- (commandline -ct) 2>/dev/null\n\
         end\n\
         complete -c cav -n '__fish_seen_subcommand_from cp' \
         -f -a '(__cav_cp_complete)'\n\
         complete -c cav -n '__fish_cav_using_subcommand agent' -s p -l project \
         -r -a '(cav __complete-projects -- (commandline -ct))'\n"
    ))
}

fn strip_lines_containing(script: &str, needle: &str) -> String {
    script
        .lines()
        .filter(|l| !l.contains(needle))
        .collect::<Vec<_>>()
        .join("\n")
        + "\n"
}

fn replace_once(haystack: &str, needle: &str, replacement: &str, shell: &str) -> Result<String> {
    if !haystack.contains(needle) {
        return Err(anchor_err(shell, needle));
    }
    Ok(haystack.replace(needle, replacement))
}

fn anchor_err(shell: &str, needle: &str) -> anyhow::Error {
    anyhow::anyhow!(
        "cav completion: anchor not found in {shell} script: {needle:?}\n\
         clap_complete's template may have changed; update completion.rs."
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use clap::CommandFactory;

    fn generated(shell: Shell) -> String {
        let mut cmd = crate::Cli::command();
        let mut buf: Vec<u8> = Vec::new();
        clap_complete::generate(shell, &mut cmd, "cav", &mut buf);
        String::from_utf8(buf).expect("clap_complete output is UTF-8")
    }

    #[test]
    fn zsh_anchors_present() {
        // Catches a clap_complete upgrade that renames or restructures
        // the cp paths action, which would silently leave users with
        // plain file completion.
        let script = inject_zsh(&generated(Shell::Zsh)).expect("zsh injection succeeds");
        assert!(script.contains("_cav_cp_paths"));
        assert!(!script.contains("'__complete-cp:"));
    }

    #[test]
    fn zsh_project_anchors_present() {
        let script = inject_zsh(&generated(Shell::Zsh)).expect("zsh injection succeeds");
        assert!(script.contains(":PATH:_cav_projects"));
        assert!(script.contains("cav __complete-projects -- "));
        assert!(!script.contains("'__complete-projects:"));
    }

    #[test]
    fn bash_anchors_present() {
        let script = inject_bash(&generated(Shell::Bash)).expect("bash injection succeeds");
        assert!(script.contains("cav __complete-cp -- "));
        assert!(!script.contains(" __complete-cp version"));
    }

    #[test]
    fn bash_project_anchors_present() {
        let script = inject_bash(&generated(Shell::Bash)).expect("bash injection succeeds");
        assert!(script.contains("_cav_complete_projects"));
        assert!(script.contains("cav __complete-projects -- "));
        assert!(!script.contains(" __complete-projects version"));
    }

    #[test]
    fn fish_anchors_present() {
        let script = inject_fish(&generated(Shell::Fish)).expect("fish injection succeeds");
        assert!(script.contains("__cav_cp_complete"));
        assert!(!script.contains("-a \"__complete-cp\""));
    }

    #[test]
    fn fish_project_anchors_present() {
        let script = inject_fish(&generated(Shell::Fish)).expect("fish injection succeeds");
        assert!(script.contains("cav __complete-projects -- (commandline -ct)"));
        assert!(!script.contains("-a \"__complete-projects\""));
    }
}
