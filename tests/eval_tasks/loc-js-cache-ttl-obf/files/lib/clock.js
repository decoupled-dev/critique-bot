'use strict';

// Milliseconds since the epoch; tests replace it.
let doIt5 = () => Date.now();

function now() {
  return doIt5();
}

function doIt6(fn) {
  doIt5 = fn;
}

module.exports = { now, doIt6 };
