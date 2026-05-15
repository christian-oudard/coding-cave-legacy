# Cave guest kernel: a slim Linux kernel built for booting under
# cloud-hypervisor. Starts from Firecracker's published reference
# config (https://s3.amazonaws.com/spec.ccfc.min/..., kept as a
# minimal-defaults base) with cave-specific additions for virtio-fs,
# vsock, PCI (CHV uses virtio-pci), and KVM host-side (subcave-as-VM).
#
# The kernel source is nixpkgs's linux_6_12 (latest LTS); make
# olddefconfig reconciles the 6.1-era reference config against it at
# build time. Drift in moved/removed CONFIG_ keys surfaces as build
# warnings.
#
# nixpkgs's stock kernel install copies bzImage but discards the raw
# vmlinux ELF. CHV's loader accepts vmlinux as a PVH-entry kernel, so
# we save it alongside.

{ pkgs }:

let
  # Symbols that must end up in the linked kernel. Cross-checks the
  # config against what was actually compiled in: Kconfig silently
  # drops `CONFIG_*=y` lines whose menuconfig parent is off (we lost
  # several iterations to `CONFIG_VIRTUALIZATION=n` masking the entire
  # KVM subtree), so an after-the-fact symbol assertion is the only
  # reliable guard.
  requiredSymbols = [
    "__pci_register_driver" # CONFIG_PCI
    "acpi_init"             # CONFIG_ACPI
    "kvm_init"              # CONFIG_KVM
    "pvpanic_pci_probe"     # CONFIG_PVPANIC_PCI
    "virtio_fs_probe"       # CONFIG_VIRTIO_FS
    "vmx_init"              # CONFIG_KVM_INTEL
  ];
in
(pkgs.linuxManualConfig {
  inherit (pkgs.linuxKernel.kernels.linux_6_12) version src;
  configfile = ./kernel.config;
  allowImportFromDerivation = true;
}).overrideAttrs (old: {
  # microvm.nix's cloud-hypervisor runner reads vmlinux from kernel.dev.
  # NixOS's initrd "modules-shrunk" step refuses to build if no module
  # tree was emitted, even when boot.initrd.includeDefaultModules is
  # false. linuxManualConfig only declares an `out` output by default,
  # so add both `dev` and `modules` here and populate them in
  # postInstall.
  outputs = [ "out" "dev" "modules" ];
  postInstall = (old.postInstall or "") + ''
    # Save vmlinux to both outputs: $out for direct cav use, $dev for
    # microvm.nix's cloud-hypervisor runner, which looks for it there.
    cp vmlinux $out/vmlinux
    install -D vmlinux $dev/vmlinux

    # Populate $modules so NixOS sees a valid output path. Every cave-
    # kernel feature is built in (=y); there are no .ko files. NixOS's
    # modules-closure script still needs (a) the versioned subdir
    # /lib/modules/<ver>/ to exist so its `ls -d *` finds a version,
    # and (b) the meta files modules.order / modules.builtin /
    # modules.builtin.modinfo to exist so the final cp's at the end of
    # the script don't error. We synthesize all three as empty.
    # Combined with boot.initrd.allowMissingModules = true, this lets
    # the closure script tolerate every modprobe missing-module result.
    mkdir -p "$modules/lib/modules/$version"
    : > "$modules/lib/modules/$version/modules.order"
    : > "$modules/lib/modules/$version/modules.builtin"
    : > "$modules/lib/modules/$version/modules.builtin.modinfo"

    missing=""
    for sym in ${pkgs.lib.escapeShellArgs requiredSymbols}; do
      if ! grep -q " $sym$" $out/System.map; then
        missing="$missing $sym"
      fi
    done
    if [ -n "$missing" ]; then
      echo "kernel: required symbols missing from build:$missing" >&2
      echo "kernel: most likely a Kconfig parent (e.g. CONFIG_VIRTUALIZATION) is off" >&2
      echo "kernel: and silently dropped the dependent =y in kernel.config." >&2
      exit 1
    fi
  '';
})
