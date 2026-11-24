import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import type { PermissionOption } from '../../src/protocol/acpTypes';
import { PermissionBroker, pickOption } from '../../src/session/permissions';

const OPTIONS: PermissionOption[] = [
  { optionId: 'sempre', name: 'Sempre nesta sessão', kind: 'allow_always' },
  { optionId: 'uma', name: 'Permitir', kind: 'allow_once' },
  { optionId: 'nao', name: 'Recusar', kind: 'reject_once' },
];

describe('pickOption', () => {
  it('Aceitar prefere allow_once; Rejeitar usa reject_once', () => {
    assert.equal(pickOption(OPTIONS, 'accept')?.optionId, 'uma');
    assert.equal(pickOption(OPTIONS, 'reject')?.optionId, 'nao');
  });

  it('cai para as variantes "always" quando necessário', () => {
    assert.equal(pickOption([{ optionId: 'x', name: 'x', kind: 'allow_always' }], 'accept')?.optionId, 'x');
    assert.equal(pickOption([{ optionId: 'y', name: 'y', kind: 'reject_always' }], 'reject')?.optionId, 'y');
    assert.equal(pickOption([], 'accept'), undefined);
  });
});

describe('PermissionBroker', () => {
  it('o primeiro a responder vence', async () => {
    const broker = new PermissionBroker();
    const resolved: Array<[string, string | undefined]> = [];
    broker.onResolved((id, opt) => resolved.push([id, opt]));
    const { permissionId, response } = broker.open('S', 't1', OPTIONS);
    assert.equal(broker.findByToolCall('t1')?.permissionId, permissionId);
    assert.equal(broker.decide(permissionId, 'accept'), true);
    assert.equal(broker.decide(permissionId, 'reject'), false);
    assert.equal(broker.select(permissionId, 'nao'), false);
    assert.deepEqual(await response, { outcome: { outcome: 'selected', optionId: 'uma' } });
    assert.deepEqual(resolved, [[permissionId, 'uma']]);
    assert.equal(broker.list().length, 0);
  });

  it('select valida o id da opção', async () => {
    const broker = new PermissionBroker();
    const { permissionId, response } = broker.open('S', 't', OPTIONS);
    assert.equal(broker.select(permissionId, 'inexistente'), false);
    assert.equal(broker.select(permissionId, 'sempre'), true);
    assert.deepEqual(await response, { outcome: { outcome: 'selected', optionId: 'sempre' } });
  });

  it('cancelAll responde cancelled só para a sessão indicada', async () => {
    const broker = new PermissionBroker();
    const a = broker.open('S1', 'a', OPTIONS);
    const b = broker.open('S2', 'b', OPTIONS);
    broker.cancelAll('S1');
    assert.deepEqual(await a.response, { outcome: { outcome: 'cancelled' } });
    assert.equal(broker.list().length, 1);
    broker.cancelAll();
    assert.deepEqual(await b.response, { outcome: { outcome: 'cancelled' } });
  });

  it('rejeitar sem opção de recusa vira cancelamento', async () => {
    const broker = new PermissionBroker();
    const { permissionId, response } = broker.open('S', 't', [{ optionId: 'ok', name: 'ok', kind: 'allow_once' }]);
    assert.equal(broker.decide(permissionId, 'reject'), true);
    assert.deepEqual(await response, { outcome: { outcome: 'cancelled' } });
  });
});
