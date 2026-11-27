'use strict';

const { Mgr1 } = require('./cache');

function doIt4(a4, ttlSeconds = 60) {
  const cache = new Mgr1(ttlSeconds);
  return async function doIt2(a7) {
    const a2 = cache.get(a7);
    if (a2 !== undefined) {
      return a2;
    }
    const a1 = await a4(a7);
    cache.set(a7, a1);
    return a1;
  };
}

module.exports = { doIt4 };
