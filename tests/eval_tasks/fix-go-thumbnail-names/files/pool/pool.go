// Package pool runs a function over many inputs with a fixed number of workers.
package pool

import (
	"sync"
)

// Map calls fn on every input using up to `workers` goroutines and returns the
// results in the same order as inputs. If any call fails, Map still waits for
// the running calls and returns the first error seen (results are then nil).
func Map[In, Out any](inputs []In, workers int, fn func(In) (Out, error)) ([]Out, error) {
	if workers < 1 {
		workers = 1
	}
	type result struct {
		out Out
		err error
	}
	jobs := make(chan In)
	results := make(chan result, len(inputs))

	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for in := range jobs {
				out, err := fn(in)
				results <- result{out: out, err: err}
			}
		}()
	}
	for _, in := range inputs {
		jobs <- in
	}
	close(jobs)
	wg.Wait()
	close(results)

	outs := make([]Out, 0, len(inputs))
	var firstErr error
	for r := range results {
		if r.err != nil && firstErr == nil {
			firstErr = r.err
		}
		outs = append(outs, r.out)
	}
	if firstErr != nil {
		return nil, firstErr
	}
	return outs, nil
}
