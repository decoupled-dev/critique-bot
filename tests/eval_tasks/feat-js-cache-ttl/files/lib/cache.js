'use strict';

/**
 * Least-recently-used cache backed by a Map (Map keeps insertion order, so the
 * first key is always the least recently used one).
 */
class LruCache {
  /**
   * @param {object} [options]
   * @param {number} [options.max=100] maximum number of entries
   * @param {() => number} [options.now=Date.now] clock, injectable for tests
   */
  constructor({ max = 100, now = Date.now } = {}) {
    if (!Number.isInteger(max) || max < 1) {
      throw new RangeError('max must be a positive integer');
    }
    this.max = max;
    this.now = now;
    this._map = new Map();
    this._hits = 0;
    this._misses = 0;
    this._evictions = 0;
  }

  get size() {
    return this._map.size;
  }

  has(key) {
    return this._map.has(key);
  }

  get(key) {
    const entry = this._map.get(key);
    if (entry === undefined) {
      this._misses++;
      return undefined;
    }
    this._map.delete(key);
    this._map.set(key, entry);
    this._hits++;
    return entry.value;
  }

  set(key, value) {
    if (this._map.has(key)) {
      this._map.delete(key);
    }
    this._map.set(key, { value, storedAt: this.now() });
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

  /** Keys from least to most recently used. */
  keys() {
    return [...this._map.keys()];
  }

  stats() {
    return { hits: this._hits, misses: this._misses, evictions: this._evictions, size: this.size };
  }
}

module.exports = { LruCache };
