import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
// @ts-expect-error pure JS observer used unchanged by the real-host fixture
import { observeFetch } from './fetch-observer.mjs';
const original = globalThis.fetch;
let work: string | undefined;
afterEach(() => { globalThis.fetch = original; if (work) rmSync(work, {recursive:true, force:true}); });
it.each(['rejected','aborted'])('RM39 records the attempted safe path even when fetch is %s', async kind => {
  work = mkdtempSync(join(tmpdir(), 'rm-action-attempt-'));
  const eventFile = join(work, 'events.jsonl'); writeFileSync(eventFile, '');
  globalThis.fetch = vi.fn(async () => { throw kind === 'aborted' ? new DOMException('stopped','AbortError') : Error('fixture failure'); });
  globalThis.fetch = observeFetch(globalThis.fetch, (event: unknown) => writeFileSync(eventFile, JSON.stringify(event)+'\n', {flag:'a'}));
  await expect(globalThis.fetch('http://127.0.0.1:12345/relay/v1/feedback?private=excluded', {
    method:'GET', headers:{authorization:'not-exported'},
  })).rejects.toThrow();
  const events = readFileSync(eventFile, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
  expect(events).toEqual([{kind:'http_attempt',method:'GET',path:'/relay/v1/feedback'}]);
  expect(readFileSync(eventFile,'utf8')).not.toContain('excluded');
  expect(readFileSync(eventFile,'utf8')).not.toContain('not-exported');
});
