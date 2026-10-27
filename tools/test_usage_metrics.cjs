'use strict';

// Pure helper tests: no DOM, application startup, network, logs or database.
// Run with: node --test tools/test_usage_metrics.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const sandbox = {window: {}};
vm.runInNewContext(
  fs.readFileSync(path.join(__dirname, '..', 'frontend', 'usage-metrics.js'), 'utf8'),
  sandbox,
  {filename: 'usage-metrics.js'},
);
const usage = sandbox.window.TLUsage;

test('raw fallback counts cache writes and excludes output/reasoning from the ratio', () => {
  const row = Object.freeze({input: 100, cache_read: 300, cache_write: 100,
    output: 10025, reasoning: 1005, total: 11530, requests: 2});
  assert.equal(usage.inputTotal(row), 500);
  assert.equal(usage.cacheRatio(row), 0.6);
  assert.equal(usage.cacheLabel(row, true), '60.0%');
  assert.equal(usage.averageTokens(row), 5765);
});

test('canonical fractional values take precedence without a second percentage conversion', () => {
  const row = {input_total: 1400, cache_hit_ratio: 3 / 14,
    avg_tokens_per_request: 10 / 3, input: 1, cache_read: 99, cache_write: 0,
    total: 999, requests: 1, cache_hit_rate: 21.4};
  assert.equal(usage.inputTotal(row), 1400);
  assert.equal(usage.cacheRatio(row), 3 / 14);
  assert.equal(usage.cacheLabel(row, true), '21.4%');
  assert.equal(usage.averageTokens(row), 10 / 3);
});

test('no input is unavailable while known input without reads is a real zero', () => {
  for (const row of [{}, {input: 0, cache_read: 0, cache_write: 0, output: 20}]) {
    assert.equal(usage.cacheRatio(row), null);
    assert.equal(usage.cacheLabel(row, true), 'N/A');
    assert.equal(usage.cacheLabel(row, false), '—');
  }
  for (const row of [{input: 10, cache_read: 0, cache_write: 0},
    {input: 0, cache_read: 0, cache_write: 10}]) {
    assert.equal(usage.cacheRatio(row), 0);
    assert.equal(usage.cacheLabel(row, true), '0.0%');
  }
  assert.equal(usage.cacheRatio({input: 0, cache_read: 10, cache_write: 0}), 1);
});

test('explicit null metrics remain unavailable even when fallback fields exist', () => {
  const row = {input: 10, cache_read: 10, cache_write: 0, total: 20, requests: 2,
    cache_hit_ratio: null, avg_tokens_per_request: null};
  assert.equal(usage.cacheRatio(row), null);
  assert.equal(usage.averageTokens(row), null);
});

test('missing raw components do not invent a denominator or reuse legacy percentage units', () => {
  for (const row of [{input: 10, cache_read: 5}, {cache_hit_rate: 60},
    {input: null, cache_read: 5, cache_write: 0}]) {
    assert.equal(usage.inputTotal(row), null);
    assert.equal(usage.cacheRatio(row), null);
  }
});

test('numeric strings preserve zero and fractional averages', () => {
  const row = {input: '100', cache_read: '300', cache_write: '100', total: '10', requests: '3'};
  assert.equal(usage.inputTotal(row), 500);
  assert.equal(usage.cacheRatio(row), 0.6);
  assert.equal(usage.averageTokens(row), 10 / 3);
  assert.equal(usage.cacheLabel({cache_hit_ratio: '0'}, true), '0.0%');
  assert.equal(usage.averageTokens({avg_tokens_per_request: '0'}), 0);
});

test('booleans, blank strings, objects and non-finite values never become valid zero metrics', () => {
  for (const value of [false, true, '', '  ', '\t', [], [0], {}, null, undefined,
    NaN, Infinity, -Infinity, 'NaN', 'Infinity']) {
    assert.equal(usage.inputTotal({input_total: value}), null);
    assert.equal(usage.cacheRatio({cache_hit_ratio: value}), null);
    assert.equal(usage.averageTokens({avg_tokens_per_request: value}), null);
    assert.equal(usage.cacheRatio({input: 10, cache_read: value, cache_write: 0}), null);
    assert.equal(usage.averageTokens({total: 30, requests: value}), null);
  }
});

test('negative counts and averages are unavailable instead of producing misleading values', () => {
  assert.equal(usage.inputTotal({input_total: -1}), null);
  assert.equal(usage.inputTotal({input: -10, cache_read: 30, cache_write: 0}), null);
  assert.equal(usage.cacheRatio({input_total: 20, cache_read: -1}), null);
  assert.equal(usage.averageTokens({avg_tokens_per_request: -1}), null);
  assert.equal(usage.averageTokens({total: -30, requests: 3}), null);
  assert.equal(usage.averageTokens({total: 30, requests: -3}), null);
});

test('zero calls are unavailable but zero total with positive calls is a real zero', () => {
  assert.equal(usage.averageTokens({total: 10, requests: 0}), null);
  assert.equal(usage.averageTokens({total: 0, requests: 5}), 0);
  assert.equal(usage.averageTokens({total: 550, requests: 4}), 137.5);
});

test('finite operands cannot leak overflow as Infinity', () => {
  assert.equal(usage.inputTotal({input: 1e308, cache_read: 1e308, cache_write: 0}), null);
  assert.equal(usage.cacheRatio({input: 1e308, cache_read: 1e308, cache_write: 0}), null);
  assert.equal(usage.averageTokens({total: 1e308, requests: 1e-308}), null);
  assert.equal(usage.inputTotal({input: Number.MAX_SAFE_INTEGER - 2, cache_read: 1, cache_write: 1}),
    Number.MAX_SAFE_INTEGER);
});

test('explicit finite ratios stay bounded to 0..1', () => {
  assert.equal(usage.cacheRatio({cache_hit_ratio: -0.1}), 0);
  assert.equal(usage.cacheRatio({cache_hit_ratio: 1.1}), 1);
});

test('tooltips explain token basis and distinguish unavailable from zero', () => {
  assert.match(usage.cacheTitle({cache_hit_ratio: 0}, true), /uncached \+ cache read \+ cache write/);
  assert.match(usage.cacheTitle({cache_hit_ratio: 0}, true), /not a request hit rate/);
  assert.match(usage.cacheTitle({cache_hit_ratio: null}, true), /No input token data/);
  assert.match(usage.cacheTitle({cache_hit_ratio: 0}, false), /未缓存输入 \+ 缓存读 \+ 缓存写/);
});
