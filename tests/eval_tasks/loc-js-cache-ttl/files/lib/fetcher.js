'use strict';

const { ResponseCache } = require('./cache');

function makeFetcher(load, ttlSeconds = 60) {
  const cache = new ResponseCache(ttlSeconds);
  return async function fetchCached(url) {
    const cached = cache.get(url);
    if (cached !== undefined) {
      return cached;
    }
    const body = await load(url);
    cache.set(url, body);
    return body;
  };
}

module.exports = { makeFetcher };
