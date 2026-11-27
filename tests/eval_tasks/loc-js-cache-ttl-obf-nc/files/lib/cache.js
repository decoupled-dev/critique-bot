'use strict';

const { now } = require('./clock');
const { doIt3 } = require('./ttl');

class Mgr1 {
  constructor(ttlSeconds) {
    this.ttlSeconds = ttlSeconds;
    this.entries = new Map();
    this.hits = 0;
    this.misses = 0;
  }

  get(key) {
    const a3 = this.entries.get(key);
    if (a3 && doIt3(a3, now())) {
      this.hits += 1;
      return a3.value;
    }
    this.misses += 1;
    return undefined;
  }

  set(key, value) {
    this.entries.set(key, { value, a6: now(), ttl: this.ttlSeconds });
  }
}

module.exports = { Mgr1 };
