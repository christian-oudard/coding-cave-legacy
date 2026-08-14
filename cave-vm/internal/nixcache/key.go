package nixcache

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Key is a nix binary-cache signing key. The on-disk format is nix's own,
// `<name>:<base64>`, so the public half can be pasted into trusted-public-keys
// unchanged and the secret half would work with `nix store sign -k`.
type Key struct {
	Name   string
	Secret ed25519.PrivateKey
}

func (k *Key) Public() string {
	return k.Name + ":" + base64.StdEncoding.EncodeToString(k.Secret.Public().(ed25519.PublicKey))
}

// Sign returns a narinfo Sig field: the key name and the signature over the
// fingerprint, which is what makes the guest's `require-sigs = true` mean
// something rather than being satisfied by anything that can reach the port.
func (k *Key) Sign(fingerprint string) string {
	return k.Name + ":" + base64.StdEncoding.EncodeToString(
		ed25519.Sign(k.Secret, []byte(fingerprint)))
}

// LoadKey reads the signing key, generating it on first use. One key per state
// dir, generated where the VM lives rather than shipped: the guest learns the
// public half through the seed disk, so a key that never leaves this machine is
// no less usable than a fleet-wide one and cannot sign for anybody else.
func LoadKey(path, name string) (*Key, error) {
	raw, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return generateKey(path, name)
	}
	if err != nil {
		return nil, err
	}
	got, b64, ok := strings.Cut(strings.TrimSpace(string(raw)), ":")
	if !ok {
		return nil, fmt.Errorf("%s: not a nix secret key (want <name>:<base64>)", path)
	}
	sec, err := base64.StdEncoding.DecodeString(b64)
	if err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if len(sec) != ed25519.PrivateKeySize {
		return nil, fmt.Errorf("%s: key is %d bytes, want %d", path, len(sec), ed25519.PrivateKeySize)
	}
	return &Key{Name: got, Secret: sec}, nil
}

func generateKey(path, name string) (*Key, error) {
	_, sec, err := ed25519.GenerateKey(nil)
	if err != nil {
		return nil, err
	}
	k := &Key{Name: name, Secret: sec}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	body := name + ":" + base64.StdEncoding.EncodeToString(sec) + "\n"
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		return nil, err
	}
	// The public half sits next to it as a plain file because everything that
	// needs it is outside this process: the seed staging in vmcav, and a human
	// checking what the guest was told to trust.
	return k, os.WriteFile(path+".pub", []byte(k.Public()+"\n"), 0o644)
}
