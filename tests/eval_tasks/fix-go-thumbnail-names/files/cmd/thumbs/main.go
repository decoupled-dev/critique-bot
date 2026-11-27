// Command thumbs runs a demo batch and prints what it would write.
package main

import (
	"flag"
	"fmt"
	"os"

	"example.com/thumbs/batch"
	"example.com/thumbs/resize"
)

func main() {
	workers := flag.Int("workers", 4, "parallel resizes")
	flag.Parse()
	images := []resize.Image{
		{Name: "photo-big.jpg", SizeKB: 4200},
		{Name: "icon.png", SizeKB: 4},
		{Name: "banner.webp", SizeKB: 900},
		{Name: "avatar.png", SizeKB: 30},
	}
	outs, err := batch.Run(images, batch.Options{Workers: *workers})
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	for _, o := range outs {
		fmt.Printf("%-20s %s\n", o.Name, o.Data)
	}
}
