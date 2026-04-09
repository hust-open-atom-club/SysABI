package main

import (
	"flag"
	"fmt"
	"os"

	"github.com/google/syzkaller/prog"
	_ "github.com/google/syzkaller/sys"
)

func main() {
	progPath := flag.String("prog", "", "path to a .syz program")
	targetOS := flag.String("os", "linux", "target OS")
	arch := flag.String("arch", "amd64", "target architecture")
	dropIndex := flag.Int("drop-index", -1, "call index to remove")
	flag.Parse()

	if *progPath == "" || *dropIndex < 0 {
		fmt.Fprintln(os.Stderr, "-prog and -drop-index are required")
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
	p, err := target.Deserialize(data, prog.NonStrict)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	if *dropIndex >= len(p.Calls) {
		fmt.Fprintf(os.Stderr, "drop index %d out of range\n", *dropIndex)
		os.Exit(1)
	}
	p.RemoveCall(*dropIndex)
	_, _ = os.Stdout.Write(p.Serialize())
}
