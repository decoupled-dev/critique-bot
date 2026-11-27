'use strict';

const { now } = require('./clock');
const { isFresh } = require('./ttl');

class ResponseCache {
  constructor(ttlSeconds) {
    this.ttlSeconds = ttlSeconds;
    this.entries = new Map();
    this.hits = 0;
    this.misses = 0;
  }

  get(key) {
    const entry = this.entries.get(key);
    if (entry && isFresh(entry, now())) {
      this.hits += 1;
      return entry.value;
    }
    this.misses += 1;
    return undefined;
  }

  set(key, value) {
    this.entries.set(key, { value, storedAt: now(), ttl: this.ttlSeconds });
  }
}

module.exports = { ResponseCache };
