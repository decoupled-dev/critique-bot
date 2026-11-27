'use strict';

/**
 * Least-recently-used cache backed by a Map (Map keeps insertion order, so the
 * first key is always the least recently used one). Entries may expire.
 */
class LruCache {
  /**
   * @param {object} [options]
   * @param {number} [options.max=100] maximum number of entries
   * @param {number} [options.ttlMs] default time-to-live in ms (none = never expire)
   * @param {() => number} [options.now=Date.now] clock, injectable for tests
   */
  constructor({ max = 100, ttlMs, now = Date.now } = {}) {
    if (!Number.isInteger(max) || max < 1) {
      throw new RangeError('max must be a positive integer');
    }
    if (ttlMs !== undefined && !(typeof ttlMs === 'number' && ttlMs > 0)) {
      throw new RangeError('ttlMs must be a positive number');
    }
    this.max = max;
    this.ttlMs = ttlMs;
    this.now = now;
    this._map = new Map();
    this._hits = 0;
    this._misses = 0;
    this._evictions = 0;
  }

  _expired(entry, at = this.now()) {
    return entry.ttlMs !== undefined && at - entry.storedAt >= entry.ttlMs;
  }

  get size() {
    this.prune();
    return this._map.size;
  }

  has(key) {
    const entry = this._map.get(key);
    if (entry === undefined) return false;
    if (this._expired(entry)) {
      this._map.delete(key);
      return false;
    }
    return true;
  }

  get(key) {
    const entry = this._map.get(key);
    if (entry === undefined || this._expired(entry)) {
      if (entry !== undefined) this._map.delete(key);
      this._misses++;
      return undefined;
    }
    this._map.delete(key);
    this._map.set(key, entry);
    this._hits++;
    return entry.value;
  }

  set(key, value, { ttlMs = this.ttlMs } = {}) {
    if (this._map.has(key)) {
      this._map.delete(key);
    }
    this._map.set(key, { value, storedAt: this.now(), ttlMs });
    while (this._map.size > this.max) {
      const oldest = this._map.keys().next().value;
      this._map.delete(oldest);
      this._evictions++;
    }
    return this;
  }

  delete(key) {
    return this._map.delete(key);
  }

  clear() {
    this._map.clear();
  }

  /** Remove expired entries; returns how many were removed. */
  prune() {
    const at = this.now();
    let removed = 0;
    for (const [key, entry] of this._map) {
      if (this._expired(entry, at)) {
        this._map.delete(key);
        removed++;
      }
    }
    return removed;
  }

  /** Keys from least to most recently used. */
  keys() {
    this.prune();
    return [...this._map.keys()];
  }

  stats() {
    return { hits: this._hits, misses: this._misses, evictions: this._evictions, size: this.size };
  }
}

module.exports = { LruCache };
