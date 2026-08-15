//! `--gpu` resolution: enumerate GPUs from `/dev/dri/by-path`, classify
//! each via `/sys/bus/pci/devices/<addr>/{vendor,class}`, and return the
//! render nodes of every discrete GPU. iGPU-only hosts get an error;
//! `-d` is still available for iGPU passthrough.
//!
//! Device nodes alone leave a cave with a GPU it cannot talk to, so this
//! module also resolves the host's driver userspace to bind alongside them.

use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::Command;

use anyhow::{anyhow, bail, Result};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Vendor {
    Nvidia,
    Intel,
    Amd,
    Other(u16),
}

impl Vendor {
    fn from_id(id: u16) -> Self {
        match id {
            0x10de => Vendor::Nvidia,
            0x8086 => Vendor::Intel,
            0x1002 => Vendor::Amd,
            other => Vendor::Other(other),
        }
    }

    fn label(&self) -> String {
        match self {
            Vendor::Nvidia => "NVIDIA".into(),
            Vendor::Intel => "Intel".into(),
            Vendor::Amd => "AMD".into(),
            Vendor::Other(id) => format!("0x{id:04x}"),
        }
    }
}

#[derive(Clone, Debug)]
pub struct Gpu {
    pub pci_addr: String,
    pub vendor: Vendor,
    pub render_node: PathBuf,
}

impl Gpu {
    /// PCI bus 0 hosts CPU-attached / chipset-attached devices, where
    /// integrated GPUs live on every consumer x86 host I've seen.
    /// Discrete GPUs sit behind a PCIe root port (bus != 0). The bus is
    /// the second `:`-separated segment of `<domain>:<bus>:<dev>.<fn>`.
    pub fn is_integrated(&self) -> bool {
        let bus = self.pci_addr.split(':').nth(1).unwrap_or("");
        u32::from_str_radix(bus, 16).map_or(false, |b| b == 0)
    }
}

fn parse_pci_addr_from_entry(name: &str) -> Option<&str> {
    let rest = name.strip_prefix("pci-")?;
    rest.strip_suffix("-render")
}

fn read_hex_prefixed(path: &Path) -> Result<u32> {
    let raw = fs::read_to_string(path)?;
    let trimmed = raw.trim();
    let hex = trimmed.strip_prefix("0x").unwrap_or(trimmed);
    u32::from_str_radix(hex, 16)
        .map_err(|e| anyhow!("parse {} = {trimmed:?}: {e}", path.display()))
}

/// PCI class 0x03xxxx covers VGA, 3D, and other display controllers, the
/// three subclasses that appear as GPUs.
fn is_display_class(class: u32) -> bool {
    (class >> 16) == 0x03
}

fn detect_in(dri_by_path: &Path, sys_pci: &Path) -> Result<Vec<Gpu>> {
    let dri_dir = dri_by_path
        .parent()
        .ok_or_else(|| anyhow!("dri by-path has no parent: {}", dri_by_path.display()))?;

    let entries = match fs::read_dir(dri_by_path) {
        Ok(it) => it,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(e) => bail!("read {}: {e}", dri_by_path.display()),
    };

    let mut gpus = Vec::new();
    for entry in entries {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        let Some(pci_addr) = parse_pci_addr_from_entry(&name) else {
            continue;
        };

        let link = fs::read_link(entry.path())?;
        let leaf = link
            .file_name()
            .ok_or_else(|| anyhow!("symlink target has no filename: {}", link.display()))?;
        let render_node = dri_dir.join(leaf);

        let pci_dir = sys_pci.join(pci_addr);
        let class = read_hex_prefixed(&pci_dir.join("class"))?;
        if !is_display_class(class) {
            continue;
        }
        let vendor_id = read_hex_prefixed(&pci_dir.join("vendor"))? as u16;

        gpus.push(Gpu {
            pci_addr: pci_addr.to_string(),
            vendor: Vendor::from_id(vendor_id),
            render_node,
        });
    }

    // Discrete GPUs first, since they're the usual pick for compute.
    // Within a kind, sort by PCI addr so order is stable across reboots.
    gpus.sort_by(|a, b| {
        a.is_integrated()
            .cmp(&b.is_integrated())
            .then_with(|| a.pci_addr.cmp(&b.pci_addr))
    });
    Ok(gpus)
}

pub fn detect() -> Result<Vec<Gpu>> {
    detect_in(
        Path::new("/dev/dri/by-path"),
        Path::new("/sys/bus/pci/devices"),
    )
}

/// CUDA needs the NVIDIA driver's control nodes alongside the DRM render
/// node: `/dev/nvidia<N>` per GPU, plus `/dev/nvidiactl`, `/dev/nvidia-uvm`,
/// `/dev/nvidia-uvm-tools`, and (when the modeset module is loaded)
/// `/dev/nvidia-modeset`. Nodes that don't exist are skipped silently so
/// hosts without the full driver still get a usable subset.
fn nvidia_extras_in(dev: &Path, gpus: &[Gpu]) -> Vec<PathBuf> {
    if !gpus.iter().any(|g| g.vendor == Vendor::Nvidia) {
        return Vec::new();
    }
    nvidia_nodes_in(dev)
}

fn nvidia_nodes_in(dev: &Path) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(entries) = fs::read_dir(dev) {
        for entry in entries.flatten() {
            let name = entry.file_name();
            let n = name.to_string_lossy();
            let Some(rest) = n.strip_prefix("nvidia") else {
                continue;
            };
            if !rest.is_empty() && rest.chars().all(|c| c.is_ascii_digit()) {
                out.push(entry.path());
            }
        }
    }
    for ctrl in &["nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"] {
        let p = dev.join(ctrl);
        if p.exists() {
            out.push(p);
        }
    }
    out.sort();
    out
}

fn discrete_gpu_paths(gpus: Vec<Gpu>, dev: &Path) -> Result<Vec<PathBuf>> {
    let discrete: Vec<Gpu> = gpus.into_iter().filter(|g| !g.is_integrated()).collect();
    if discrete.is_empty() {
        bail!(
            "--gpu: no discrete GPU detected under /dev/dri/by-path. \
             Use -d /dev/dri/renderD12X to bind an integrated GPU."
        );
    }
    let mut paths: Vec<PathBuf> = discrete.iter().map(|g| g.render_node.clone()).collect();
    paths.extend(nvidia_extras_in(dev, &discrete));
    Ok(paths)
}

/// Inside a cave there is no `/dev/dri/by-path` and no `/sys`, so a GPU
/// cannot be classified here. It doesn't need to be: the host cave bound
/// bare nodes for GPUs it had already vetted, so `--gpu` in a subcave
/// means "hand down every GPU node I was given".
fn cave_gpu_paths(dev: &Path) -> Result<Vec<PathBuf>> {
    let mut paths = Vec::new();
    if let Ok(entries) = fs::read_dir(dev.join("dri")) {
        for entry in entries.flatten() {
            if entry.file_name().to_string_lossy().starts_with("renderD") {
                paths.push(entry.path());
            }
        }
    }
    paths.sort();
    paths.extend(nvidia_nodes_in(dev));
    if paths.is_empty() {
        bail!("--gpu: no GPU device nodes in this cave. Relaunch the host cave with --gpu.");
    }
    Ok(paths)
}

fn in_cave() -> bool {
    std::env::var_os("CODING_CAVE_VERSION").is_some()
}

/// Resolve `--gpu` to the device-node paths needed for every discrete
/// GPU on the host: DRM render nodes plus vendor-specific control nodes
/// (NVIDIA's `/dev/nvidia*`). Deterministic; same behavior interactive
/// and non-interactive.
pub fn resolve() -> Result<Vec<PathBuf>> {
    if in_cave() {
        return cave_gpu_paths(Path::new("/dev"));
    }
    discrete_gpu_paths(detect()?, Path::new("/dev"))
}

/// The host's graphics driver userspace. NixOS merges the vendor libraries,
/// the Vulkan ICD manifest and the GLVND EGL vendor manifest here, and both
/// the Vulkan loader and libglvnd search this path by default, so binding
/// the tree is all a cave needs -- no env wiring.
const DRIVER_TREE: &str = "/run/opengl-driver";

/// Bind sources for the driver userspace, each paired with its mount point.
///
/// A cave cannot build its own copy: NVIDIA's userspace libraries must match
/// the running kernel module exactly, and the cave's nixpkgs pin is a
/// different driver release. Without this the loader finds no ICD and
/// reports no Vulkan devices, and CUDA finds no libcuda.
///
/// The tree is a symlink farm into the host store while a cave's
/// `/nix/store` is its own private store, so the closure has to come along
/// for the symlinks and every RUNPATH inside it to resolve. In a cave the
/// closure is already there: the inherited `/nix/store` bind is recursive,
/// so the host cave's driver mounts come with it.
pub fn driver_binds() -> Result<Vec<(PathBuf, PathBuf)>> {
    let tree = Path::new(DRIVER_TREE);
    if !tree.exists() {
        bail!(
            "--gpu: {DRIVER_TREE} is missing, so the cave would get GPU device \
             nodes with no driver to drive them. Enable hardware.graphics on the host."
        );
    }
    let real = fs::canonicalize(tree)?;
    let mut binds: Vec<(PathBuf, PathBuf)> = if in_cave() {
        Vec::new()
    } else {
        closure(&real)?.into_iter().map(|p| (p.clone(), p)).collect()
    };
    binds.push((real, PathBuf::from(DRIVER_TREE)));
    Ok(binds)
}

fn closure(path: &Path) -> Result<Vec<PathBuf>> {
    let out = Command::new("nix-store")
        .args(["--query", "--requisites"])
        .arg(path)
        .output()?;
    if !out.status.success() {
        bail!(
            "--gpu: nix-store --query --requisites {}: {}",
            path.display(),
            String::from_utf8_lossy(&out.stderr).trim()
        );
    }
    Ok(String::from_utf8(out.stdout)?
        .lines()
        .map(PathBuf::from)
        .collect())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::symlink;

    struct Fixture {
        _root: PathBuf,
        dri_by_path: PathBuf,
        sys_pci: PathBuf,
    }

    impl Fixture {
        fn new(label: &str) -> Self {
            let tmp = std::env::temp_dir();
            let mut root = PathBuf::new();
            for i in 0..1000 {
                let p = tmp.join(format!("cav-gpu-test-{label}-{i}"));
                if fs::create_dir(&p).is_ok() {
                    root = p;
                    break;
                }
            }
            assert!(!root.as_os_str().is_empty());
            let dri = root.join("dev/dri");
            let by_path = dri.join("by-path");
            let sys = root.join("sys/bus/pci/devices");
            fs::create_dir_all(&by_path).unwrap();
            fs::create_dir_all(&sys).unwrap();
            Self { _root: root, dri_by_path: by_path, sys_pci: sys }
        }

        fn add_gpu(&self, pci: &str, render_leaf: &str, vendor: u32, class: u32) {
            let node = self.dri_by_path.parent().unwrap().join(render_leaf);
            fs::write(&node, b"").unwrap();
            let link = self.dri_by_path.join(format!("pci-{pci}-render"));
            symlink(format!("../{render_leaf}"), &link).unwrap();
            // Also add a -card entry to mirror real hosts; detect should ignore it.
            let card_leaf = render_leaf.replace("renderD", "card");
            let card_node = self.dri_by_path.parent().unwrap().join(&card_leaf);
            fs::write(&card_node, b"").unwrap();
            let card_link = self.dri_by_path.join(format!("pci-{pci}-card"));
            symlink(format!("../{card_leaf}"), &card_link).unwrap();

            let pci_dir = self.sys_pci.join(pci);
            fs::create_dir_all(&pci_dir).unwrap();
            fs::write(pci_dir.join("vendor"), format!("0x{vendor:04x}\n")).unwrap();
            fs::write(pci_dir.join("class"), format!("0x{class:06x}\n")).unwrap();
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self._root);
        }
    }

    #[test]
    fn detect_empty_when_missing() {
        let f = Fixture::new("empty");
        fs::remove_dir(&f.dri_by_path).unwrap();
        let v = detect_in(&f.dri_by_path, &f.sys_pci).unwrap();
        assert!(v.is_empty());
    }

    #[test]
    fn detect_puts_discrete_before_integrated() {
        let f = Fixture::new("two");
        // Add integrated first to prove order is sort-driven, not
        // insertion-driven.
        f.add_gpu("0000:00:02.0", "renderD128", 0x8086, 0x030000);
        f.add_gpu("0000:01:00.0", "renderD129", 0x10de, 0x030200);
        let v = detect_in(&f.dri_by_path, &f.sys_pci).unwrap();
        assert_eq!(v.len(), 2);
        assert_eq!(v[0].vendor, Vendor::Nvidia);
        assert!(!v[0].is_integrated());
        assert_eq!(v[1].vendor, Vendor::Intel);
        assert!(v[1].is_integrated());
    }

    #[test]
    fn detect_skips_non_display_class() {
        let f = Fixture::new("nondisplay");
        // 0x040300 = audio device; should be skipped even with a render-style symlink.
        f.add_gpu("0000:02:00.0", "renderD130", 0x10de, 0x040300);
        let v = detect_in(&f.dri_by_path, &f.sys_pci).unwrap();
        assert!(v.is_empty());
    }

    #[test]
    fn vendor_unknown_carries_raw_id() {
        assert_eq!(Vendor::from_id(0xabcd), Vendor::Other(0xabcd));
        assert_eq!(Vendor::from_id(0xabcd).label(), "0xabcd");
    }

    fn make(pci: &str, vendor: Vendor, leaf: &str) -> Gpu {
        Gpu {
            pci_addr: pci.into(),
            vendor,
            render_node: PathBuf::from(format!("/dev/dri/{leaf}")),
        }
    }

    struct TempDir(PathBuf);
    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }
    fn empty_dev(label: &str) -> TempDir {
        let tmp = std::env::temp_dir();
        for i in 0..1000 {
            let p = tmp.join(format!("cav-gpu-dev-{label}-{i}"));
            if fs::create_dir(&p).is_ok() {
                return TempDir(p);
            }
        }
        panic!("could not allocate tmp dir");
    }
    impl TempDir {
        fn path(&self) -> &Path { &self.0 }
    }

    #[test]
    fn discrete_only_returns_all_discrete() {
        let dev = empty_dev("discrete-only");
        let nodes = discrete_gpu_paths(
            vec![
                make("0000:00:02.0", Vendor::Intel, "renderD128"),
                make("0000:01:00.0", Vendor::Amd, "renderD130"),
            ],
            dev.path(),
        )
        .unwrap();
        assert_eq!(nodes, vec![PathBuf::from("/dev/dri/renderD130")]);
    }

    #[test]
    fn discrete_error_when_only_integrated() {
        let dev = empty_dev("only-integrated");
        let err = discrete_gpu_paths(
            vec![make("0000:00:02.0", Vendor::Intel, "renderD128")],
            dev.path(),
        )
        .unwrap_err();
        let msg = err.to_string();
        assert!(msg.contains("no discrete GPU"));
        assert!(msg.contains("-d /dev/dri/renderD12X"));
    }

    #[test]
    fn nvidia_extras_added_when_nvidia_discrete() {
        let dev = empty_dev("nvidia-extras");
        for f in &["nvidia0", "nvidiactl", "nvidia-uvm", "nvidia-uvm-tools"] {
            fs::write(dev.path().join(f), b"").unwrap();
        }
        let paths = discrete_gpu_paths(
            vec![make("0000:01:00.0", Vendor::Nvidia, "renderD128")],
            dev.path(),
        )
        .unwrap();
        let names: Vec<String> = paths
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert!(names.contains(&"renderD128".to_string()));
        assert!(names.contains(&"nvidia0".to_string()));
        assert!(names.contains(&"nvidiactl".to_string()));
        assert!(names.contains(&"nvidia-uvm".to_string()));
        assert!(names.contains(&"nvidia-uvm-tools".to_string()));
    }

    #[test]
    fn cave_passes_every_gpu_node_down() {
        // No /dev/dri/by-path and no /sys in a cave: classification is the
        // host cave's job and already happened.
        let dev = empty_dev("cave-nodes");
        fs::create_dir(dev.path().join("dri")).unwrap();
        for f in &["renderD128", "card0"] {
            fs::write(dev.path().join("dri").join(f), b"").unwrap();
        }
        for f in &["nvidia0", "nvidiactl", "nvidia-uvm"] {
            fs::write(dev.path().join(f), b"").unwrap();
        }
        let paths = cave_gpu_paths(dev.path()).unwrap();
        let names: Vec<String> = paths
            .iter()
            .map(|p| p.file_name().unwrap().to_string_lossy().into_owned())
            .collect();
        assert_eq!(
            names,
            vec!["renderD128", "nvidia-uvm", "nvidia0", "nvidiactl"]
        );
    }

    #[test]
    fn cave_without_gpu_nodes_errors() {
        let dev = empty_dev("cave-empty");
        let e = cave_gpu_paths(dev.path()).unwrap_err();
        assert!(e.to_string().contains("no GPU device nodes"));
    }

    #[test]
    fn no_nvidia_extras_for_amd_only() {
        let dev = empty_dev("amd-only");
        for f in &["nvidia0", "nvidiactl"] {
            fs::write(dev.path().join(f), b"").unwrap();
        }
        let paths = discrete_gpu_paths(
            vec![make("0000:01:00.0", Vendor::Amd, "renderD130")],
            dev.path(),
        )
        .unwrap();
        assert_eq!(paths, vec![PathBuf::from("/dev/dri/renderD130")]);
    }
}
