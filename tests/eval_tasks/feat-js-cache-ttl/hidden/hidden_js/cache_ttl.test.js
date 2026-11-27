'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { LruCache } = require(path.join(process.cwd(), 'lib', 'cache.js'));

function clock(start = 1000) {
  const c = { t: start };
  c.now = () => c.t;
  return c;
}

test('entries expire after default ttl', () => {
  const k = clock();
  const c = new LruCache({ max: 10, ttlMs: 100, now: k.now });
  c.set('a', 1);
  k.t += 99;
  assert.equal(c.get('a'), 1);
  assert.equal(c.has('a'), true);
  k.t += 1; // age == ttl -> expired
  assert.equal(c.get('a'), undefined);
  assert.equal(c.has('a'), false);
  assert.equal(c.stats().misses, 1);
  assert.equal(c.stats().hits, 1);
});

test('no ttl means no expiry', () => {
  const k = clock();
  const c = new LruCache({ max: 3, now: k.now });
  c.set('a', 1);
  k.t += 1e9;
  assert.equal(c.get('a'), 1);
  assert.equal(c.size, 1);
});

test('per-entry ttl overrides default', () => {
  const k = clock();
  const c = new LruCache({ ttlMs: 1000, now: k.now });
  c.set('short', 's', { ttlMs: 10 });
  c.set('long', 'l');
  c.set('forever', 'f', { ttlMs: undefined });
  k.t += 10;
  assert.equal(c.get('short'), undefined);
  assert.equal(c.get('long'), 'l');
  k.t += 5000;
  assert.equal(c.get('long'), undefined);
  const plain = new LruCache({ now: k.now });
  plain.set('x', 1, { ttlMs: 50 });
  k.t += 50;
  assert.equal(plain.has('x'), false);
});

test('size and keys ignore expired entries', () => {
  const k = clock();
  const c = new LruCache({ max: 10, ttlMs: 100, now: k.now });
  c.set('a', 1);
  k.t += 60;
  c.set('b', 2);
  c.set('c', 3, { ttlMs: 1000 });
  k.t += 50; // a expired (110), b alive (50)
  assert.equal(c.size, 2);
  assert.deepEqual(c.keys(), ['b', 'c']);
  k.t += 100;
  assert.equal(c.size, 1);
  assert.deepEqual(c.keys(), ['c']);
  assert.equal(c.stats().size, 1);
});

test('prune returns number removed', () => {
  const k = clock();
  const c = new LruCache({ max: 10, ttlMs: 100, now: k.now });
  c.set('a', 1).set('b', 2).set('c', 3, { ttlMs: 500 });
  assert.equal(c.prune(), 0);
  k.t += 100;
  assert.equal(c.prune(), 2);
  assert.equal(c.prune(), 0);
  assert.deepEqual(c.keys(), ['c']);
});

test('set again restarts ttl, get does not extend it', () => {
  const k = clock();
  const c = new LruCache({ ttlMs: 100, now: k.now });
  c.set('a', 1);
  k.t += 80;
  c.set('a', 2);
  k.t += 80;
  assert.equal(c.get('a'), 2);
  k.t += 19;
  assert.equal(c.get('a'), 2);
  k.t += 1;
  assert.equal(c.get('a'), undefined);
});

test('get still updates lru order', () => {
  const k = clock();
  const c = new LruCache({ max: 2, ttlMs: 1000, now: k.now });
  c.set('a', 1).set('b', 2);
  c.get('a');
  c.set('c', 3);
  assert.deepEqual(c.keys(), ['a', 'c']);
  assert.equal(c.stats().evictions, 1);
});

test('existing behaviour unchanged', () => {
  const c = new LruCache({ max: 2 });
  c.set('a', 1).set('b', 2);
  assert.equal(c.get('a'), 1);
  assert.throws(() => new LruCache({ max: 0 }), RangeError);
  assert.equal(c.delete('a'), true);
  assert.equal(c.size, 1);
});
