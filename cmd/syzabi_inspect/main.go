package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/google/syzkaller/prog"
	_ "github.com/google/syzkaller/sys"
)

type output struct {
	ProgramID                     string   `json:"program_id"`
	NormalizedSyz                 string   `json:"normalized_syz"`
	Arch                          string   `json:"arch"`
	TargetOS                      string   `json:"target_os"`
	SyscallList                   []string `json:"syscall_list"`
	FullSyscallList               []string `json:"full_syscall_list"`
	ResourceClasses               []string `json:"resource_classes"`
	UsesPseudoSyscalls            bool     `json:"uses_pseudo_syscalls"`
	UsesThreadingSensitiveFeature bool     `json:"uses_threading_sensitive_features"`
	CallCount                     int      `json:"call_count"`
}

func main() {
	progPath := flag.String("prog", "", "path to a .syz program")
	targetOS := flag.String("os", "linux", "target OS")
	arch := flag.String("arch", "amd64", "target architecture")
	strict := flag.Bool("strict", false, "strict parse mode")
	flag.Parse()

	if *progPath == "" {
		fmt.Fprintln(os.Stderr, "-prog is required")
		os.Exit(1)
	}
	target, err := prog.GetTarget(*targetOS, *arch)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	data, err := os.ReadFile(*progPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	mode := prog.NonStrict
	if *strict {
		mode = prog.Strict
	}
	p, err := target.Deserialize(data, mode)
	if err != nil {
		fmt.Fprintf(os.Stderr, "parse_error: %v\n", err)
		os.Exit(2)
	}

	normalized := strings.TrimSpace(string(p.Serialize())) + "\n"
	sum := sha256.Sum256([]byte(normalized))
	out := output{
		ProgramID:       hex.EncodeToString(sum[:]),
		NormalizedSyz:   normalized,
		Arch:            *arch,
		TargetOS:        *targetOS,
		CallCount:       len(p.Calls),
		SyscallList:     uniqueBaseCalls(p),
		FullSyscallList: collectFullCalls(p),
		ResourceClasses: collectResourceClasses(p),
	}
	out.UsesPseudoSyscalls = hasPseudo(p)
	out.UsesThreadingSensitiveFeature = hasThreadSensitiveProps(p)

	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func uniqueBaseCalls(p *prog.Prog) []string {
	seen := make(map[string]struct{})
	var list []string
	for _, call := range p.Calls {
		name := call.Meta.CallName
		if _, ok := seen[name]; ok {
			continue
		}
		seen[name] = struct{}{}
		list = append(list, name)
	}
	sort.Strings(list)
	return list
}

func collectFullCalls(p *prog.Prog) []string {
	seen := make(map[string]struct{})
	var list []string
	for _, call := range p.Calls {
		name := call.Meta.Name
		if _, ok := seen[name]; ok {
			continue
		}
		seen[name] = struct{}{}
		list = append(list, name)
	}
	sort.Strings(list)
	return list
}

func collectResourceClasses(p *prog.Prog) []string {
	seen := make(map[string]struct{})
	for _, call := range p.Calls {
		prog.ForeachArg(call, func(arg prog.Arg, _ *prog.ArgCtx) {
			if res, ok := arg.Type().(*prog.ResourceType); ok {
				seen[res.TypeName] = struct{}{}
			}
		})
	}
	var list []string
	for name := range seen {
		list = append(list, name)
	}
	sort.Strings(list)
	return list
}

func hasPseudo(p *prog.Prog) bool {
	for _, call := range p.Calls {
		if strings.HasPrefix(call.Meta.CallName, "syz_") {
			return true
		}
	}
	return false
}

func hasThreadSensitiveProps(p *prog.Prog) bool {
	for _, call := range p.Calls {
		if call.Props.Async || call.Props.FailNth > 0 || call.Props.Rerun > 0 {
			return true
		}
	}
	return false
}
