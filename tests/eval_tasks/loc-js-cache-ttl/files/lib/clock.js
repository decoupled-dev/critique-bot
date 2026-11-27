'use strict';

// Milliseconds since the epoch; tests replace it.
let source = () => Date.now();

function now() {
  return source();
}

function useClock(fn) {
  source = fn;
}

module.exports = { now, useClock };
