package main

import (
	"fmt"
	"os"

	"example.com/gallery/thumbs"
)

func main() {
	store := &thumbs.Store{Dir: "thumbs-out"}
	for _, photo := range os.Args[1:] {
		where, err := store.Save(photo, 256, []byte("png"))
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		fmt.Println(where)
	}
}
