/*
 * Copyright 2026 Kenny Root
 * Licensed under the Apache License, Version 2.0.
 */

const DAY = 24 * 60 * 60 * 1000;
const STALE_DAYS = 180;
const CLOSE_DAYS = 30;
const STALE_LABEL = 'stale';
const WARNING_MARKER = '<!-- connectbot-stale-warning -->';
const MEMBERS = new Set(['OWNER', 'MEMBER', 'COLLABORATOR']);

function isExempt(issue) {
  return issue.state !== 'open' || issue.pull_request || issue.locked ||
    issue.milestone ||
    issue.labels.some(label => label.name.toLowerCase() === 'keep-open');
}

function isWarning(comment) {
  return comment.user?.login === 'github-actions[bot]' &&
    comment.body?.startsWith(WARNING_MARKER);
}

function decide(issue, comments, now) {
  const reviewed = comments.some(comment => MEMBERS.has(comment.author_association) &&
    comment.user?.type !== 'Bot' && !comment.user?.login?.endsWith('[bot]'));
  if (isExempt(issue) || !reviewed) {
    return 'skip';
  }

  const activityAt = Math.max(
    Date.parse(issue.updated_at),
    ...comments.filter(comment => !isWarning(comment)).map(comment => Date.parse(comment.updated_at)),
  );
  const stale = issue.labels.some(label => label.name === STALE_LABEL);
  if (!stale) {
    return now - activityAt >= STALE_DAYS * DAY ? 'mark' : 'skip';
  }

  // Never close an issue based only on a manually applied stale label.
  const warning = comments.filter(isWarning).at(-1);
  if (!warning) return 'skip';

  const warnedAt = Date.parse(warning.created_at);
  if (activityAt > warnedAt) return 'unmark';
  return now - warnedAt >= CLOSE_DAYS * DAY ? 'close' : 'skip';
}

async function run({ github, context, core, dryRun = true, now = Date.now() }) {
  const repo = context.repo;
  const api = github.rest.issues;
  // Take a complete snapshot first: mutations must not reorder pagination.
  const issues = await github.paginate(api.listForRepo, {
    ...repo, state: 'open', sort: 'created', direction: 'asc', per_page: 100,
  });
  const summary = { mark: 0, close: 0, unmark: 0, skip: 0 };
  let labelReady = false;

  for (const candidate of issues) {
    if (isExempt(candidate)) {
      summary.skip++;
      continue;
    }
    const stale = candidate.labels.some(label => label.name === STALE_LABEL);
    if (!stale && now - Date.parse(candidate.updated_at) < STALE_DAYS * DAY) {
      summary.skip++;
      continue;
    }

    const target = { ...repo, issue_number: candidate.number };
    // Paginate the entire history to establish prior member review.
    // API failures stop the run rather than treating missing history as empty.
    const comments = await github.paginate(api.listComments, { ...target, per_page: 100 });
    const { data: issue } = await api.get(target);
    const action = decide(issue, comments, now);
    summary[action]++;
    core.info(`${dryRun ? '[dry run] ' : ''}#${candidate.number}: ${action}`);
    if (dryRun || action === 'skip') continue;

    if (action === 'mark') {
      if (!labelReady) {
        try {
          await api.getLabel({ ...repo, name: STALE_LABEL });
        } catch (error) {
          if (error.status !== 404) throw error;
          await api.createLabel({
            ...repo, name: STALE_LABEL, color: 'ededed',
            description: 'Inactive issue awaiting a response before automatic closure',
          });
        }
        labelReady = true;
      }
      await api.addLabels({ ...target, labels: [STALE_LABEL] });
      try {
        await api.createComment({
          ...target,
          body: `${WARNING_MARKER}\nThanks for the report. This issue has had no activity ` +
            `for ${STALE_DAYS} days. If it still affects you, please comment with any ` +
            `updates so we can keep it open. Otherwise, it will be closed after another ` +
            `${CLOSE_DAYS} days without activity. A maintainer can reopen it if needed.`,
        });
      } catch (error) {
        // A failed warning must not leave an issue scheduled for closure.
        await api.removeLabel({ ...target, name: STALE_LABEL });
        throw error;
      }
    } else if (action === 'unmark') {
      await api.removeLabel({ ...target, name: STALE_LABEL });
    } else if (action === 'close') {
      // Comments were checked again this run, including before closure.
      await api.update({ ...target, state: 'closed', state_reason: 'not_planned' });
    }
  }

  core.info(JSON.stringify(summary));
  return summary;
}

export { run, decide, WARNING_MARKER };
