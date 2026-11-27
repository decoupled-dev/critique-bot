'use strict';

// An entry is fresh while less than its time-to-live has passed since it was stored.
// storedAt is in milliseconds; ttl comes from the config in seconds.
function isFresh(entry, nowMs) {
  return nowMs - entry.storedAt < entry.ttl;
}

function expiresAt(entry) {
  return entry.storedAt + entry.ttl * 1000;
}

module.exports = { isFresh, expiresAt };
