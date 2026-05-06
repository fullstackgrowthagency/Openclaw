import { execFileSync } from 'node:child_process';
import fs from 'node:fs';

const manifest = JSON.parse(fs.readFileSync(new URL('../data/generated/social-week-2026-05-07.json', import.meta.url), 'utf8'));
const workdir = new URL('..', import.meta.url).pathname;
const approvedNote = 'Approved in chat: create this week\'s social posts for Full Stack Growth Agency.';
const alreadyCreatedDates = new Set(['2026-05-10T16:00:00.000Z']);

for (const post of manifest.posts) {
  if (alreadyCreatedDates.has(post.scheduleDate)) {
    console.log(JSON.stringify({ skipped: true, scheduleDate: post.scheduleDate, reason: 'already_created' }));
    continue;
  }

  const payload = {
    mutationRequest: {
      action: 'create_social_post',
      locationId: manifest.locationId,
      summary: post.summary,
      status: 'in_review',
      scheduleDate: post.scheduleDate,
      accountIds: [manifest.accountId],
      post: {
        type: 'post',
        userId: 'btIGyS5g6wMTrqaoNKhy',
        summary: post.summary,
        status: 'in_review',
        scheduleDate: post.scheduleDate,
        accountIds: [manifest.accountId],
        media: []
      }
    }
  };

  const runRaw = execFileSync('node', [
    'src/cli/index.js',
    'taskpack:run',
    '--name=marketing_asset_pack',
    '--event-type=ManualRun',
    `--location-id=${manifest.locationId}`,
    '--company-id=JHs3QUBxdSpQYbwF85Ts',
    '--credential-ref=location-zO0CeZsgcBbBsOgRCaJq-pit',
    `--payload-json=${JSON.stringify(payload)}`
  ], { cwd: workdir, encoding: 'utf8' });
  const run = JSON.parse(runRaw);
  console.log(JSON.stringify({ scheduleDate: post.scheduleDate, runId: run.runId, status: run.status }));

  if (run.status !== 'awaiting_approval') {
    throw new Error(`Expected awaiting_approval for ${post.scheduleDate}, got ${run.status}`);
  }

  const approveRaw = execFileSync('node', [
    'src/cli/index.js',
    'approve',
    '--decider=Hagen',
    `--note=${approvedNote}`
  ], { cwd: workdir, encoding: 'utf8' });
  const approval = JSON.parse(approveRaw);
  console.log(JSON.stringify({ scheduleDate: post.scheduleDate, approvalId: approval.approval?.id || null, resumedStatus: approval.resumed?.status || null }));
}
