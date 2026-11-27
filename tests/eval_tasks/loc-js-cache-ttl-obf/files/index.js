'use strict';

const { doIt4 } = require('./lib/fetcher');

const doIt2 = doIt4(async (a7) => `body of ${a7}`, 60);
doIt2('/api/weather').then((a1) => console.log(a1));
