package main

import (
	"fmt"
	"os"

	"example.com/gallery/thumbs"
)

func main() {
	a9 := &thumbs.Store{A1: "thumbs-out"}
	for _, a6 := range os.Args[1:] {
		where, a4 := a9.DoIt1(a6, 256, []byte("png"))
		if a4 != nil {
			fmt.Fprintln(os.Stderr, a4)
			os.Exit(1)
		}
		fmt.Println(where)
	}
}
