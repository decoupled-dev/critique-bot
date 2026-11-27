'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { LruCache } = require('../lib/cache');

test('stores and returns values', () => {
  const c = new LruCache({ max: 2 });
  c.set('a', 1).set('b', 2);
  assert.equal(c.get('a'), 1);
  assert.equal(c.size, 2);
});

test('evicts least recently used', () => {
  const c = new LruCache({ max: 2 });
  c.set('a', 1).set('b', 2);
  c.get('a');
  c.set('c', 3);
  assert.deepEqual(c.keys(), ['a', 'c']);
  assert.equal(c.stats().evictions, 1);
});

test('counts hits and misses', () => {
  const c = new LruCache();
  c.set('x', 1);
  c.get('x');
  c.get('y');
  assert.deepEqual(c.stats(), { hits: 1, misses: 1, evictions: 0, size: 1 });
});

test('rejects bad max', () => {
  assert.throws(() => new LruCache({ max: 0 }), RangeError);
});
