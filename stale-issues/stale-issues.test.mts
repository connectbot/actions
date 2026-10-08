/*
 * Copyright 2026 Kenny Root
 * Licensed under the Apache License, Version 2.0.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { run, decide, WARNING_MARKER } from './stale-issues.mts';
const DAY = 24 * 60 * 60 * 1000;
const now = Date.parse('2026-10-08T00:00:00Z');
const ago = days => new Date(now - days * DAY).toISOString();

function issue(overrides = {}) {
  return {
    number: 1, state: 'open', author_association: 'NONE',
    updated_at: ago(200), labels: [], ...overrides,
  };
}

function comment(overrides = {}) {
  return {
    body: 'Still affects me', user: { login: 'reporter' },
    author_association: 'NONE', created_at: ago(190), updated_at: ago(190), ...overrides,
  };
}

function review(overrides = {}) {
  return comment({
    author_association: 'MEMBER', created_at: ago(300), updated_at: ago(300), ...overrides,
  });
}

function warning(days = 31, overrides = {}) {
  return comment({
    user: { login: 'github-actions[bot]', type: 'Bot' }, body: `${WARNING_MARKER}\nWarning`,
    created_at: ago(days), updated_at: ago(days), ...overrides,
  });
}

function stale(overrides = {}) {
  return issue({ labels: [{ name: 'stale' }], updated_at: ago(31), ...overrides });
}

for (const association of ['OWNER', 'MEMBER', 'COLLABORATOR']) {
  test(`${association} review enables warning and closure after inactivity`, () => {
    const reply = review({ author_association: association });
    assert.equal(decide(issue(), [reply], now), 'mark');
    assert.equal(decide(stale(), [reply, warning()], now), 'close');
    assert.equal(decide(issue({ author_association: association }), [], now), 'skip');
  });
}

test('unreviewed issues are never warned or closed', () => {
  for (const comments of [
    [], [comment()], [comment({ author_association: 'CONTRIBUTOR' })],
    [review({ user: { login: 'app', type: 'Bot' } })],
    [review({ user: { login: 'app[bot]' } })],
  ]) {
    assert.equal(decide(issue(), comments, now), 'skip');
    assert.equal(decide(stale(), [...comments, warning()], now), 'skip');
  }
});

test('milestones, keep-open, locked issues and pull requests are exempt', () => {
  for (const override of [
    { milestone: { number: 13 } }, { labels: [{ name: 'keep-open' }] },
    { locked: true }, { pull_request: {} }, { state: 'closed' },
  ]) assert.equal(decide(issue(override), [review()], now), 'skip');
});

test('warn only after 180 days, including ordinary contributor replies', () => {
  assert.equal(decide(issue({ updated_at: ago(179) }), [review()], now), 'skip');
  assert.equal(decide(issue({ updated_at: ago(180) }), [review()], now), 'mark');
  assert.equal(decide(issue(), [review(), comment({ author_association: 'CONTRIBUTOR' })], now), 'mark');
});

test('closure needs a genuine bot warning and a full 30 days', () => {
  assert.equal(decide(stale({ updated_at: ago(29) }), [review(), warning(29)], now), 'skip');
  assert.equal(decide(stale({ updated_at: ago(30) }), [review(), warning(30)], now), 'close');
  assert.equal(decide(stale(), [review()], now), 'skip');
  assert.equal(decide(stale(), [review(), warning(31, { user: { login: 'reporter' } })], now), 'skip');
});

test('reporter activity and issue edits reset the stale cycle', () => {
  assert.equal(decide(stale({ updated_at: ago(1) }), [review(), warning()], now), 'unmark');
  assert.equal(decide(stale(), [review(), warning(), comment({ updated_at: ago(1) })], now), 'unmark');
});

function client({ current = issue(), comments = [review()], failHistory = false, failWarning = false } = {}) {
  const calls = [];
  const api = Object.fromEntries([
    'listForRepo', 'listComments', 'getLabel', 'createLabel', 'addLabels',
    'removeLabel', 'createComment', 'update',
  ].map(name => [name, async args => {
    calls.push({ name, args });
    if (name === 'createComment' && failWarning) throw new Error('warning failed');
    return { data: {} };
  }]));
  api.get = async () => ({ data: current });
  const github = {
    rest: { issues: api },
    paginate: async (method, args) => {
      assert.equal(args.per_page, 100);
      if (method === api.listForRepo) return [issue()];
      assert.equal(method, api.listComments);
      if (failHistory) throw new Error('history unavailable');
      return comments;
    },
  };
  return { github, calls };
}

async function execute(options = {}, dryRun = false) {
  const { github, calls } = client(options);
  const summary = await run({
    github, context: { repo: { owner: 'connectbot', repo: 'connectbot' } },
    core: { info() {} }, dryRun, now,
  });
  return { calls, summary };
}

test('a member beyond the first 100 comments establishes review', async () => {
  const comments = Array.from({ length: 101 }, () => comment());
  comments.push(review());
  const { calls, summary } = await execute({ comments });
  assert.deepEqual(calls.map(c => c.name), ['getLabel', 'addLabels', 'createComment']);
  assert.equal(summary.mark, 1);
});

test('fresh member replies restart inactivity and remove stale labels', async () => {
  const reply = review({ updated_at: ago(1) });
  assert.equal(decide(issue(), [reply], now), 'skip');
  const { calls } = await execute({ current: stale(), comments: [review(), warning(), reply] });
  assert.deepEqual(calls.map(c => c.name), ['removeLabel']);
});

test('unreviewed, milestone and recently active issues receive no writes', async () => {
  for (const options of [
    { current: stale(), comments: [warning()] },
    { current: issue(), comments: [] },
    { current: stale({ milestone: { number: 13 } }), comments: [review(), warning()] },
    { current: issue({ updated_at: ago(1) }), comments: [] },
  ]) assert.deepEqual((await execute(options)).calls, []);
});

test('dry run makes no writes even when a warning is due', async () => {
  const { calls, summary } = await execute({}, true);
  assert.deepEqual(calls, []);
  assert.equal(summary.mark, 1);
});

test('warning adds the stale label and explains the grace period', async () => {
  const { calls } = await execute();
  assert.deepEqual(calls.map(c => c.name), ['getLabel', 'addLabels', 'createComment']);
  assert.match(calls.at(-1).args.body, /180 days/);
  assert.match(calls.at(-1).args.body, /30 days/);
});

test('closure uses the warning timer and a recoverable close reason', async () => {
  const { calls } = await execute({ current: stale(), comments: [review(), warning()] });
  assert.deepEqual(calls.map(c => c.name), ['update']);
  assert.equal(calls[0].args.state, 'closed');
  assert.equal(calls[0].args.state_reason, 'not_planned');
});

test('comment lookup failure cannot lead to a warning or closure', async () => {
  const { github, calls } = client({ failHistory: true });
  await assert.rejects(run({
    github, context: { repo: { owner: 'connectbot', repo: 'connectbot' } },
    core: { info() {} }, dryRun: false, now,
  }), /history unavailable/);
  assert.deepEqual(calls, []);
});

test('failed warning rolls back its stale label', async () => {
  const { github, calls } = client({ failWarning: true });
  await assert.rejects(run({
    github, context: { repo: { owner: 'connectbot', repo: 'connectbot' } },
    core: { info() {} }, dryRun: false, now,
  }), /warning failed/);
  assert.deepEqual(calls.map(c => c.name), ['getLabel', 'addLabels', 'createComment', 'removeLabel']);
});
