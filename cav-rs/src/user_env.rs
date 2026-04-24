use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{bail, Result};

use crate::config;
use crate::constants;

fn cave_nix_path() -> PathBuf {
    let host_home = std::env::var("CODING_CAVE_HOST_HOME")
        .unwrap_or_else(|_| std::env::var("HOME").unwrap_or_default());
    PathBuf::from(host_home)
        .join(".config")
        .join("coding-cave")
        .join("cave.nix")
}

fn user_env_dir() -> PathBuf {
    config::data_dir().join("user-env")
}

pub fn user_env_result_path() -> PathBuf {
    user_env_dir().join("result")
}

fn hash_file() -> PathBuf {
    config::data_dir().join("user-env.hash")
}

fn compute_hash(cave_nix_content: Option<&str>, sibling_files: &[(String, String)]) -> String {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let build = constants::build();
    let parts = [
        build.nixpkgs_url.as_str(),
        build.claude_code_nix_url.as_str(),
        build.home_manager_url.as_str(),
        build.coding_cave_url.as_str(),
        cave_nix_content.unwrap_or(""),
    ];
    let mut hasher = DefaultHasher::new();
    for part in &parts {
        part.hash(&mut hasher);
    }
    let mut sorted_siblings: Vec<&(String, String)> = sibling_files.iter().collect();
    sorted_siblings.sort_by(|a, b| a.0.cmp(&b.0));
    for (name, content) in &sorted_siblings {
        name.hash(&mut hasher);
        content.hash(&mut hasher);
    }
    format!("{:x}", hasher.finish())
}

fn needs_build(cave_nix_content: Option<&str>, sibling_files: &[(String, String)]) -> bool {
    let result_link = user_env_dir().join("result");
    if !result_link.exists() {
        return true;
    }
    let hf = hash_file();
    if !hf.exists() {
        return true;
    }
    let stored = std::fs::read_to_string(&hf).unwrap_or_default();
    stored.trim() != compute_hash(cave_nix_content, sibling_files)
}

fn to_nix(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => {
            let escaped = s
                .replace('\\', "\\\\")
                .replace('"', "\\\"")
                .replace("${", "\\${");
            format!("\"{escaped}\"")
        }
        serde_json::Value::Bool(b) => if *b { "true" } else { "false" }.to_string(),
        serde_json::Value::Object(map) => {
            let items: Vec<String> = map
                .iter()
                .map(|(k, v)| format!("{k} = {}", to_nix(v)))
                .collect();
            format!("{{ {}; }}", items.join("; "))
        }
        serde_json::Value::Array(arr) => {
            let items: Vec<String> = arr.iter().map(to_nix).collect();
            format!("[ {} ]", items.join(" "))
        }
        _ => value.to_string(),
    }
}

fn extract_user_inputs(cave_nix_dest: &Path) -> serde_json::Value {
    let r = Command::new("nix")
        .args([
            "eval",
            "--json",
            "--impure",
            "--expr",
            &format!("(import {}).inputs or {{}}", cave_nix_dest.display()),
        ])
        .output();
    match r {
        Ok(out) if out.status.success() => {
            serde_json::from_slice(&out.stdout).unwrap_or(serde_json::json!({}))
        }
        Ok(out) => {
            eprint!("{}", String::from_utf8_lossy(&out.stderr));
            serde_json::json!({})
        }
        Err(_) => serde_json::json!({}),
    }
}

const SYSTEM_INPUT_NAMES: &[&str] = &["self", "nixpkgs", "claude-code", "home-manager", "coding-cave"];

fn generate_flake(has_cave_nix: bool, user_inputs: &serde_json::Value) -> String {
    let build = constants::build();
    let mut out = String::new();
    out.push_str("{\n");
    out.push_str(&format!("  inputs.nixpkgs.url = \"{}\";\n", build.nixpkgs_url));
    out.push_str("  inputs.claude-code = {\n");
    out.push_str(&format!("    url = \"{}\";\n", build.claude_code_nix_url));
    out.push_str("    inputs.nixpkgs.follows = \"nixpkgs\";\n  };\n");
    out.push_str("  inputs.home-manager = {\n");
    out.push_str(&format!("    url = \"{}\";\n", build.home_manager_url));
    out.push_str("    inputs.nixpkgs.follows = \"nixpkgs\";\n  };\n");
    out.push_str("  inputs.coding-cave = {\n");
    out.push_str(&format!("    url = \"{}\";\n", build.coding_cave_url));
    out.push_str("    inputs.nixpkgs.follows = \"nixpkgs\";\n");
    out.push_str("    inputs.home-manager.follows = \"home-manager\";\n  };\n");

    if let serde_json::Value::Object(map) = user_inputs {
        for (name, spec) in map {
            if SYSTEM_INPUT_NAMES.contains(&name.as_str()) {
                continue;
            }
            if let serde_json::Value::String(url) = spec {
                out.push_str(&format!("  inputs.{name}.url = \"{url}\";\n"));
            } else {
                out.push_str(&format!("  inputs.{name} = {};\n", to_nix(spec)));
            }
        }
    }

    out.push_str("\n  outputs = { nixpkgs, claude-code, home-manager, coding-cave, ... }@inputs:\n");
    if has_cave_nix {
        out.push_str("    let\n");
        out.push_str("      userCfg = import ./cave.nix;\n");
        out.push_str("      userInputs = builtins.removeAttrs inputs\n");
        let mut system_names: Vec<&str> = SYSTEM_INPUT_NAMES.to_vec();
        system_names.sort();
        let names_nix = format!(
            "[ {} ]",
            system_names.iter().map(|s| format!("\"{s}\"")).collect::<Vec<_>>().join(" ")
        );
        out.push_str(&format!("        {names_nix};\n"));
        out.push_str("    in coding-cave.lib.mkCaveEnv {\n");
        out.push_str("      inherit claude-code;\n");
        out.push_str("      userModule = { pkgs, ... }:\n");
        out.push_str("        userCfg.config (userInputs // { inherit pkgs; });\n");
        out.push_str("    };\n");
    } else {
        out.push_str("    coding-cave.lib.mkCaveEnv {\n");
        out.push_str("      inherit claude-code;\n");
        out.push_str("      userModule = { ... }: { };\n");
        out.push_str("    };\n");
    }
    out.push_str("}\n");
    out
}

fn copy_sibling_nix_files(
    cave_nix_src: &Path,
    user_env: &Path,
) -> Result<Vec<(String, String)>> {
    let mut sibling_files = vec![];
    let src_dir = match cave_nix_src.parent() {
        Some(d) => d.to_path_buf(),
        None => return Ok(sibling_files),
    };
    if !src_dir.is_dir() {
        return Ok(sibling_files);
    }
    let cave_nix_name = cave_nix_src.file_name().unwrap_or_default();
    for entry in std::fs::read_dir(&src_dir)?.flatten() {
        if entry.file_name() == cave_nix_name {
            continue;
        }
        let path = entry.path();
        if path.is_file() && path.extension().is_some_and(|e| e == "nix") {
            let name = entry.file_name().to_string_lossy().to_string();
            let content = std::fs::read_to_string(&path)?;
            std::fs::write(user_env.join(&name), &content)?;
            sibling_files.push((name, content));
        }
    }
    Ok(sibling_files)
}

fn clean_stale_sibling_files(user_env: &Path, current_names: &[String]) -> Result<()> {
    let keep: std::collections::HashSet<&str> = current_names
        .iter()
        .map(|s| s.as_str())
        .chain(["flake.nix", "cave.nix"].iter().copied())
        .collect();
    for entry in std::fs::read_dir(user_env)?.flatten() {
        let path = entry.path();
        if path.is_file() && path.extension().is_some_and(|e| e == "nix") {
            let name = entry.file_name().to_string_lossy().to_string();
            if !keep.contains(name.as_str()) {
                std::fs::remove_file(&path).ok();
            }
        }
    }
    Ok(())
}

fn prepare() -> Result<(Option<String>, Vec<(String, String)>)> {
    let user_env = user_env_dir();
    std::fs::create_dir_all(&user_env)?;

    let cave_nix = cave_nix_path();
    let cave_nix_dest = user_env.join("cave.nix");

    let (cave_nix_content, sibling_files, flake) = if cave_nix.exists() {
        let content = std::fs::read_to_string(&cave_nix)?;
        std::fs::write(&cave_nix_dest, &content)?;
        let siblings = copy_sibling_nix_files(&cave_nix, &user_env)?;
        let user_inputs = extract_user_inputs(&cave_nix_dest);
        let flake = generate_flake(true, &user_inputs);
        (Some(content), siblings, flake)
    } else {
        let _ = std::fs::remove_file(&cave_nix_dest);
        let flake = generate_flake(false, &serde_json::json!({}));
        (None, vec![], flake)
    };

    let sibling_names: Vec<String> = sibling_files.iter().map(|(n, _)| n.clone()).collect();
    clean_stale_sibling_files(&user_env, &sibling_names)?;
    std::fs::write(user_env.join("flake.nix"), &flake)?;
    Ok((cave_nix_content, sibling_files))
}

fn do_build(user_env: &Path) -> Option<PathBuf> {
    let _ = std::fs::remove_dir_all("/homeless-shelter");
    // Capture stdout for --print-out-paths; let stderr inherit so the
    // user sees nix's live progress during a slow rebuild.
    let result = Command::new("nix")
        .args(["build", "--no-link", "--print-out-paths"])
        .current_dir(user_env)
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::inherit())
        .output();
    match result {
        Ok(out) if out.status.success() => {
            let out_path_str = String::from_utf8_lossy(&out.stdout);
            let out_path = PathBuf::from(out_path_str.lines().next().unwrap_or("").trim());
            let result_link = user_env.join("result");
            let old_path = result_link
                .read_link()
                .ok()
                .map(|p| p.to_string_lossy().to_string());
            let _ = std::fs::remove_file(&result_link);
            let _ = std::os::unix::fs::symlink(&out_path, &result_link);
            if old_path.as_deref() != Some(&out_path.to_string_lossy()) {
                eprintln!("Cave environment updated.");
            }
            Some(out_path)
        }
        _ => {
            eprintln!("Warning: cave environment build failed.");
            None
        }
    }
}

fn read_sibling_files() -> Vec<(String, String)> {
    let cave_nix = cave_nix_path();
    if !cave_nix.exists() {
        return vec![];
    }
    let src_dir = match cave_nix.parent() {
        Some(d) => d.to_path_buf(),
        None => return vec![],
    };
    if !src_dir.is_dir() {
        return vec![];
    }
    let cave_nix_name = cave_nix.file_name().unwrap_or_default().to_os_string();
    let mut files = vec![];
    if let Ok(entries) = std::fs::read_dir(&src_dir) {
        for entry in entries.flatten() {
            if entry.file_name() == cave_nix_name {
                continue;
            }
            let path = entry.path();
            if path.is_file() && path.extension().is_some_and(|e| e == "nix") {
                if let Ok(content) = std::fs::read_to_string(&path) {
                    files.push((entry.file_name().to_string_lossy().to_string(), content));
                }
            }
        }
    }
    files
}

pub fn build() -> Result<()> {
    let cave_nix = cave_nix_path();
    let cave_nix_content = if cave_nix.exists() {
        std::fs::read_to_string(&cave_nix).ok()
    } else {
        None
    };
    let sibling_files = read_sibling_files();

    if !needs_build(cave_nix_content.as_deref(), &sibling_files) {
        return Ok(());
    }

    crate::log_info!("building user env");
    let (content, siblings) = prepare()?;
    if do_build(&user_env_dir()).is_some() {
        let hash = compute_hash(content.as_deref(), &siblings);
        std::fs::write(hash_file(), hash)?;
    }
    Ok(())
}

pub fn update(input_name: Option<&str>) -> Result<()> {
    prepare()?;
    let user_env = user_env_dir();
    let mut cmd = Command::new("nix");
    cmd.args(["flake", "update"]).current_dir(&user_env);
    if let Some(name) = input_name {
        cmd.arg(name);
    }
    let status = cmd.status()?;
    if !status.success() {
        bail!("nix flake update failed.");
    }
    eprintln!("Run 'cav env build' to rebuild with updated inputs.");
    Ok(())
}

pub fn metadata() -> Result<()> {
    prepare()?;
    Command::new("nix")
        .args(["flake", "metadata"])
        .current_dir(user_env_dir())
        .status()?;
    Ok(())
}

pub fn default_cave_nix() -> Result<String> {
    Ok(r#"{
  config = { pkgs, ... }: {
    home.packages = with pkgs; [
      jq gawk findutils curl wget nano
    ];

    home.sessionVariables = {
      EDITOR = "nano";
    };
  };
}
"#
    .to_string())
}
