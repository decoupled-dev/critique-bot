'use strict';

const { makeFetcher } = require('./lib/fetcher');

const fetchCached = makeFetcher(async (url) => `body of ${url}`, 60);
fetchCached('/api/weather').then((body) => console.log(body));
