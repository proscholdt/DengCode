import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { AcpClient, readDengcodeCapability } from '../../src/protocol/acpClient';
import { JsonRpcConnection } from '../../src/protocol/jsonrpc';

interface Sent {
  id?: number | string;
  method?: string;
  params?: Record<string, unknown>;
  result?: unknown;
  error?: { code: number; message: string };
}

function setup(): { client: AcpClient; conn: JsonRpcConnection; sent: Sent[]; reply: (result: unknown) => void } {
  const sent: Sent[] = [];
  const conn = new JsonRpcConnection((line) => sent.push(JSON.parse(line) as Sent));
  const client = new AcpClient(conn, { name: 'teste', version: '0.1.0' });
  const reply = (result: unknown): void => {
    const last = [...sent].reverse().find((m) => m.method && m.id !== undefined);
    conn.receive(`${JSON.stringify({ jsonrpc: '2.0', id: last?.id, result })}\n`);
  };
  return { client, conn, sent, reply };
}

const tick = (): Promise<void> => new Promise((r) => setImmediate(r));

const INIT_OK = {
  protocolVersion: 1,
  agentCapabilities: {
    loadSession: false,
    _meta: { dengcode: { version: 1, extensions: ['sqlApproval', 'dataPreview', 'lineage', 'stack', 'workspace'] } },
  },
  agentInfo: { name: 'dengcode', version: '0.1.0' },
};

describe('AcpClient', () => {
  it('initialize anuncia v1 sem fs e sem terminal', async () => {
    const { client, sent, reply } = setup();
    const p = client.initialize();
    assert.equal(sent[0]?.method, 'initialize');
    assert.deepEqual(sent[0]?.params, {
      protocolVersion: 1,
      clientCapabilities: { fs: { readTextFile: false, writeTextFile: false }, terminal: false },
      clientInfo: { name: 'teste', version: '0.1.0' },
    });
    reply(INIT_OK);
    await p;
    assert.equal(client.supports('lineage'), true);
    assert.equal(client.dengcode?.version, 1);
  });

  it('recusa versão de protocolo diferente', async () => {
    const { client, reply } = setup();
    const p = client.initialize();
    reply({ protocolVersion: 2 });
    await assert.rejects(p, /ACP v2/);
  });

  it('sessão, prompt e cancelamento seguem o ACP', async () => {
    const { client, sent, reply } = setup();
    const s = client.newSession('/proj');
    assert.deepEqual(sent[0], { jsonrpc: '2.0', id: 1, method: 'session/new', params: { cwd: '/proj', mcpServers: [] } });
    reply({ sessionId: 'S1' });
    assert.equal((await s).sessionId, 'S1');
    const p = client.prompt('S1', [{ type: 'text', text: 'oi' }]);
    assert.deepEqual(sent[1]?.params, { sessionId: 'S1', prompt: [{ type: 'text', text: 'oi' }] });
    client.cancel('S1');
    assert.deepEqual(sent[2], { jsonrpc: '2.0', method: 'session/cancel', params: { sessionId: 'S1' } });
    reply({ stopReason: 'cancelled' });
    assert.equal((await p).stopReason, 'cancelled');
  });

  it('despacha session/update e as notificações _dengcode', () => {
    const { client, conn } = setup();
    const got: string[] = [];
    client.setHandlers({
      onSessionUpdate: (n) => got.push(`update:${n.update.sessionUpdate}`),
      onSqlApproval: (p) => got.push(`sql:${p.kind}`),
      onDataPreview: (p) => got.push(`preview:${p.rows.length}`),
      onUnknownNotification: (m) => got.push(`ignorada:${m}`),
    });
    const send = (method: string, params: unknown): void => conn.receive(`${JSON.stringify({ jsonrpc: '2.0', method, params })}\n`);
    send('session/update', { sessionId: 'S', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'a' } } });
    send('_dengcode/sqlApproval', { sessionId: 'S', toolCallId: 't', sql: 'select 1', kind: 'READ' });
    send('_dengcode/dataPreview', { sessionId: 'S', toolCallId: 't', columns: [], rows: [{}, {}] });
    send('_outro/coisa', {});
    assert.deepEqual(got, ['update:agent_message_chunk', 'sql:READ', 'preview:2', 'ignorada:_outro/coisa']);
  });

  it('session/request_permission responde com a decisão do handler', async () => {
    const { client, conn, sent } = setup();
    client.setHandlers({
      onRequestPermission: async (req) => ({ outcome: { outcome: 'selected', optionId: req.options[0]?.optionId ?? '' } }),
    });
    conn.receive(
      `${JSON.stringify({
        jsonrpc: '2.0',
        id: 9,
        method: 'session/request_permission',
        params: { sessionId: 'S', toolCall: { toolCallId: 't' }, options: [{ optionId: 'ok', name: 'Permitir', kind: 'allow_once' }] },
      })}\n`,
    );
    await tick();
    assert.deepEqual(sent[0], { jsonrpc: '2.0', id: 9, result: { outcome: { outcome: 'selected', optionId: 'ok' } } });
  });

  it('sem handler de permissão, cancela; fs/* não é suportado', async () => {
    const { conn, sent } = setup();
    conn.receive(
      `${JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'session/request_permission', params: { sessionId: 'S', toolCall: { toolCallId: 't' }, options: [] } })}\n`,
    );
    conn.receive(`${JSON.stringify({ jsonrpc: '2.0', id: 2, method: 'fs/read_text_file', params: { path: '/x' } })}\n`);
    await tick();
    assert.deepEqual(sent[0]?.result, { outcome: { outcome: 'cancelled' } });
    assert.equal(sent[1]?.error?.code, -32601);
  });

  it('extensões só são chamadas se anunciadas', async () => {
    const { client, sent, reply } = setup();
    await assert.rejects(client.lineage({ sessionId: 'S', target: 'a', direction: 'down', depth: 2 }), /não anuncia/);
    const init = client.initialize();
    reply(INIT_OK);
    await init;
    const p = client.lineage({ sessionId: 'S', target: 'silver.pedidos', direction: 'down', depth: 2 });
    assert.equal(sent[1]?.method, '_dengcode/lineage');
    assert.deepEqual(sent[1]?.params, { sessionId: 'S', target: 'silver.pedidos', direction: 'down', depth: 2 });
    reply({ root: 'silver.pedidos', nodes: [], edges: [] });
    assert.equal((await p).root, 'silver.pedidos');
    const s = client.stackUse('local', 'prod');
    assert.deepEqual(sent[2]?.params, { stack: 'local', env: 'prod' });
    reply({ stack: 'local', env: 'prod' });
    await s;
    const w = client.workspaceUse('cliente-a');
    assert.equal(sent[3]?.method, '_dengcode/workspace.use');
    reply({ workspace: 'cliente-a' });
    await w;
  });

  it('readDengcodeCapability é defensivo', () => {
    assert.equal(readDengcodeCapability(undefined), undefined);
    assert.equal(readDengcodeCapability({ protocolVersion: 1, agentCapabilities: {} }), undefined);
    assert.deepEqual(
      readDengcodeCapability({ protocolVersion: 1, agentCapabilities: { _meta: { dengcode: { extensions: ['lineage', 3] } } } }),
      { version: 0, extensions: ['lineage'] },
    );
  });
});
