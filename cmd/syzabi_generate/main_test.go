package main

import (
	"testing"

	"github.com/google/syzkaller/prog"
)

func TestIsAllowedSyscallMatchesFullName(t *testing.T) {
	allowSet := parseAllowlist("openat,read")
	if !isAllowedSyscall(&prog.Syscall{Name: "openat", CallName: "openat"}, allowSet) {
		t.Fatalf("expected exact full syscall name to be allowed")
	}
	if isAllowedSyscall(&prog.Syscall{Name: "openat$fuse", CallName: "openat"}, allowSet) {
		t.Fatalf("expected specialized syscall variant to be rejected")
	}
	if isAllowedSyscall(&prog.Syscall{Name: "read$FUSE", CallName: "read"}, allowSet) {
		t.Fatalf("expected specialized read variant to be rejected")
	}
}

func TestIsAllowedSyscallAllowsEverythingWithoutAllowlist(t *testing.T) {
	if !isAllowedSyscall(&prog.Syscall{Name: "pipe2$watch_queue", CallName: "pipe2"}, nil) {
		t.Fatalf("expected empty allowlist to allow syscall")
	}
}
