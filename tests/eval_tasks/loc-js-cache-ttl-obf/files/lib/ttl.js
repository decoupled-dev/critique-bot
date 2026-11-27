'use strict';

// An entry is fresh while less than its time-to-live has passed since it was stored.
// storedAt is in milliseconds; ttl comes from the config in seconds.
function doIt3(a3, a5) {
  return a5 - a3.a6 < a3.ttl;
}

function doIt1(a3) {
  return a3.a6 + a3.ttl * 1000;
}

module.exports = { doIt3, doIt1 };
