import test from 'node:test';
import assert from 'node:assert/strict';
import { GatewayError, parseCommand, replyFor, responseChannel } from '../src/bus.ts';

test('parses the commands.py envelope, with and without v and args', () => {
  const id = 'a'.repeat(32);
  assert.deepEqual(parseCommand(JSON.stringify({ command_id: id, action: 'status', args: {} })), { command_id: id, action: 'status', args: {} });
  assert.deepEqual(parseCommand(JSON.stringify({ command_id: id, action: 'status' })), { command_id: id, action: 'status', args: {} });
  assert.deepEqual(parseCommand(JSON.stringify({ command_id: id, action: 'open', args: { epoch: 1 }, v: 1 })), { command_id: id, action: 'open', args: { epoch: 1 } });
});

test('rejects malformed envelopes instead of throwing', () => {
  assert.equal(parseCommand('not json'), null);
  assert.equal(parseCommand('[]'), null);
  assert.equal(parseCommand(JSON.stringify({ action: 'x' })), null);
  assert.equal(parseCommand(JSON.stringify({ command_id: 'zz', action: 'x' })), null);
  assert.equal(parseCommand(JSON.stringify({ command_id: 'ab', action: '' })), null);
  assert.equal(parseCommand(JSON.stringify({ command_id: 'ab', action: 'x', v: 2 })), null);
  assert.equal(parseCommand(JSON.stringify({ command_id: 'ab', action: 'x', args: [1] })), null);
});

test('replies: ok/result, and "<Kind>: <detail>" + error_kind for failures', () => {
  assert.deepEqual(replyFor({ result: { state: 'opening' } }), { ok: true, result: { state: 'opening' } });
  assert.deepEqual(replyFor({ result: undefined }), { ok: true, result: null });
  assert.deepEqual(replyFor({ error: new GatewayError('stale_epoch', 'lease epoch is 3, command carries 2') }), {
    ok: false,
    error: 'stale_epoch: lease epoch is 3, command carries 2',
    error_kind: 'stale_epoch',
  });
  assert.deepEqual(replyFor({ error: new TypeError('boom') }), { ok: false, error: 'TypeError: boom', error_kind: 'other' });
  assert.equal(responseChannel('abc'), 'cmdresp:abc');
});
