package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPrivateHealthConfiguration(t *testing.T) {
	root := t.TempDir()
	defaults, err := loadHealthTargets(root)
	if err != nil || defaults.HTTP == "" || defaults.SSH != "" {
		t.Fatal("default must work without publishing an SSH server address")
	}
	for _, data := range []string{
		`{"ssh_target":"ssh.campus.example:22"}`,
		`{"http_target":"","ssh_target":"[fd00::1]:22"}`,
	} {
		if err := os.WriteFile(filepath.Join(root, "health.json"), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := loadHealthTargets(root); err != nil {
			t.Fatal(err)
		}
	}
	for _, data := range []string{
		`{"http_target":"","ssh_target":""}`,
		`{"ssh_target":"host:0"}`,
		`{"ssh_target":"host:65536"}`,
		`{"ssh_target":"host\r\nInjected:22"}`,
		`{"http_target":"https://campus.example"}`,
		`{"unknown":"value"}`,
		`{} {}`,
	} {
		if err := os.WriteFile(filepath.Join(root, "health.json"), []byte(data), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := loadHealthTargets(root); err == nil {
			t.Fatalf("accepted invalid configuration: %s", data)
		}
	}
}

func TestRuntimePathIsPerUser(t *testing.T) {
	t.Setenv("FUDANGATEWAY_ATRUST_HOME", t.TempDir())
	if defaultBase() != os.Getenv("FUDANGATEWAY_ATRUST_HOME") {
		t.Fatal("explicit runtime was ignored")
	}
}
