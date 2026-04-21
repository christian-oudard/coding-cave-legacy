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
    clap_complete::generate(sh, &mut cmd, "cav", &mut std::io::stdout());
    Ok(())
}
