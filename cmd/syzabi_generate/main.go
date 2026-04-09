package main

import (
	"crypto/sha256"
	"encoding/hex"
	"flag"
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/google/syzkaller/prog"
	_ "github.com/google/syzkaller/sys"
)

func main() {
	targetOS := flag.String("os", "linux", "target OS")
	arch := flag.String("arch", "amd64", "target architecture")
	outputDir := flag.String("output-dir", "", "output directory")
	count := flag.Int("count", 1000, "number of programs")
	seed := flag.Int64("seed", 1, "deterministic seed")
	length := flag.Int("length", 4, "preferred calls per program")
	allow := flag.String("allow", "", "comma-separated full syscall allowlist")
	flag.Parse()

	if *outputDir == "" {
		fmt.Fprintln(os.Stderr, "-output-dir is required")
		os.Exit(1)
	}

	target, err := prog.GetTarget(*targetOS, *arch)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	allowSet := parseAllowlist(*allow)
	enabled := make(map[*prog.Syscall]bool)
	for _, call := range target.Syscalls {
		if isAllowedSyscall(call, allowSet) {
			enabled[call] = true
		}
	}
	choiceTable := target.BuildChoiceTable(nil, enabled)

	if err := os.MkdirAll(*outputDir, 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	seen := make(map[string]struct{})
	generated := 0
	for attempt := 0; generated < *count && attempt < *count*200; attempt++ {
		rs := rand.NewSource(*seed + int64(attempt))
		p := target.Generate(rs, *length, choiceTable)
		normalized := strings.TrimSpace(string(p.Serialize())) + "\n"
		sum := sha256.Sum256([]byte(normalized))
		programID := hex.EncodeToString(sum[:])
		if _, ok := seen[programID]; ok {
			continue
		}
		seen[programID] = struct{}{}
		filename := filepath.Join(*outputDir, fmt.Sprintf("%04d-%s.syz", generated, shortID(programID)))
		if err := os.WriteFile(filename, []byte(normalized), 0o644); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		generated++
	}

	if generated != *count {
		var ids []string
		for id := range seen {
			ids = append(ids, id)
		}
		sort.Strings(ids)
		fmt.Fprintf(os.Stderr, "generated %d/%d unique programs\n", generated, *count)
		os.Exit(2)
	}
}

func shortID(value string) string {
	if len(value) < 12 {
		return value
	}
	return value[:12]
}

func parseAllowlist(raw string) map[string]struct{} {
	allowSet := make(map[string]struct{})
	for _, item := range strings.Split(raw, ",") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		allowSet[item] = struct{}{}
	}
	return allowSet
}

func isAllowedSyscall(call *prog.Syscall, allowSet map[string]struct{}) bool {
	if len(allowSet) == 0 {
		return true
	}
	_, ok := allowSet[call.Name]
	return ok
}
