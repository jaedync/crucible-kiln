// node --test tools/web_tests/*.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { getJSON } from '../../src/kiln/web/static/js/api.js';

test('null and undefined params are left out of the query string', async () => {
  const urls = [];
  globalThis.fetch = async (url) => { urls.push(url); return new Response('{}', { status: 200 }); };
  // chart.js sends host: tick && tick.host, which is null before the first tick.
  await getJSON('series', { range: '1h', group: 'none', host: null });
  await getJSON('series', { range: '1h', group: 'none', host: undefined });
  await getJSON('series', { range: '1h', group: 'none', host: 'strix' });
  assert.deepEqual(urls, ['api/v1/series?range=1h&group=none', 'api/v1/series?range=1h&group=none',
    'api/v1/series?range=1h&group=none&host=strix']);
});
