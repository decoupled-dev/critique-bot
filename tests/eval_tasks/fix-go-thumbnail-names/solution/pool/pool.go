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
	type job struct {
		idx int
		in  In
	}
	type result struct {
		idx int
		out Out
		err error
	}
	jobs := make(chan job)
	results := make(chan result, len(inputs))

	var wg sync.WaitGroup
	for w := 0; w < workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range jobs {
				out, err := fn(j.in)
				results <- result{idx: j.idx, out: out, err: err}
			}
		}()
	}
	for i, in := range inputs {
		jobs <- job{idx: i, in: in}
	}
	close(jobs)
	wg.Wait()
	close(results)

	outs := make([]Out, len(inputs))
	var firstErr error
	for r := range results {
		if r.err != nil && firstErr == nil {
			firstErr = r.err
		}
		outs[r.idx] = r.out
	}
	if firstErr != nil {
		return nil, firstErr
	}
	return outs, nil
}
