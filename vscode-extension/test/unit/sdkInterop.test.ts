/**
 * Interoperabilidade: o nosso cliente conversa com um agente feito com a
 * biblioteca oficial do ACP (`@agentclientprotocol/sdk`). Garante que o
 * enquadramento e as formas das mensagens batem com a implementação de
 * referência — não só com o que nós mesmos achamos que é o protocolo.
 */

import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import * as acp from '@agentclientprotocol/sdk';
import { AcpClient } from '../../src/protocol/acpClient';
import type { SessionNotification, SqlApprovalParams } from '../../src/protocol/acpTypes';
import { JsonRpcConnection } from '../../src/protocol/jsonrpc';

function pipePair(): { conn: JsonRpcConnection; stream: acp.Stream } {
  const encoder = new TextEncoder();
  let toAgent!: ReadableStreamDefaultController<Uint8Array>;
  const agentInput = new ReadableStream<Uint8Array>({
    start(controller) {
      toAgent = controller;
    },
  });
  const conn = new JsonRpcConnection((line) => toAgent.enqueue(encoder.encode(line)));
  const agentOutput = new WritableStream<Uint8Array>({
    write(chunk) {
      conn.receive(chunk);
    },
  });
  return { conn, stream: acp.ndJsonStream(agentOutput, agentInput) };
}

describe('interoperabilidade com a biblioteca oficial do ACP', () => {
  it('handshake, sessão, streaming, extensão e permissão', async () => {
    const { conn, stream } = pipePair();
    let permissionAnswer: unknown;
    const agentApp = acp
      .agent({ name: 'agente-sdk' })
      .onRequest(acp.methods.agent.initialize, () => ({
        protocolVersion: acp.PROTOCOL_VERSION,
        agentCapabilities: { _meta: { dengcode: { version: 1, extensions: ['sqlApproval', 'lineage'] } } },
        agentInfo: { name: 'agente-sdk', version: '9.9.9' },
      }))
      .onRequest(acp.methods.agent.session.new, () => ({ sessionId: 'sessao-sdk' }))
      .onRequest(acp.methods.agent.session.prompt, async ({ params, client }) => {
        const text = params.prompt.map((b) => (b.type === 'text' ? b.text : '')).join('');
        await client.notify(acp.methods.client.session.update, {
          sessionId: params.sessionId,
          update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: `eco: ${text}` } },
        });
        await client.notify(acp.methods.client.session.update, {
          sessionId: params.sessionId,
          update: {
            sessionUpdate: 'tool_call',
            toolCallId: 'tc1',
            title: 'query_run',
            kind: 'execute',
            status: 'pending',
            content: [{ type: 'diff', path: '/p/a.sql', oldText: 'a', newText: 'b' }],
          },
        });
        await client.notify('_dengcode/sqlApproval', {
          sessionId: params.sessionId,
          toolCallId: 'tc1',
          sql: 'DELETE FROM t',
          kind: 'DML',
        });
        permissionAnswer = await client.request(acp.methods.client.session.requestPermission, {
          sessionId: params.sessionId,
          toolCall: { toolCallId: 'tc1' },
          options: [
            { optionId: 'sim', name: 'Permitir', kind: 'allow_once' },
            { optionId: 'nao', name: 'Recusar', kind: 'reject_once' },
          ],
        });
        return { stopReason: 'end_turn' };
      })
      .onRequest('_dengcode/lineage', (p: unknown) => p as { target: string }, ({ params }) => ({
        root: params.target,
        nodes: [{ id: params.target, kind: 'table' }],
        edges: [],
      }));
    const connection = agentApp.connect(stream);

    const client = new AcpClient(conn, { name: 'cliente-teste', version: '0.1.0' });
    const updates: SessionNotification[] = [];
    const sql: SqlApprovalParams[] = [];
    client.setHandlers({
      onSessionUpdate: (n) => updates.push(n),
      onSqlApproval: (p) => sql.push(p),
      onRequestPermission: async (req) => {
        const reject = req.options.find((o) => o.kind === 'reject_once');
        return { outcome: { outcome: 'selected', optionId: reject?.optionId ?? '' } };
      },
    });

    const init = await client.initialize();
    assert.equal(init.agentInfo?.name, 'agente-sdk');
    assert.equal(client.supports('lineage'), true);

    const session = await client.newSession('/projeto');
    assert.equal(session.sessionId, 'sessao-sdk');

    const result = await client.prompt(session.sessionId, [{ type: 'text', text: 'olá' }]);
    assert.equal(result.stopReason, 'end_turn');
    assert.deepEqual(
      updates.map((u) => u.update.sessionUpdate),
      ['agent_message_chunk', 'tool_call'],
    );
    assert.equal(sql[0]?.kind, 'DML');
    assert.deepEqual(permissionAnswer, { outcome: { outcome: 'selected', optionId: 'nao' } });

    const graph = await client.lineage({ sessionId: 'sessao-sdk', target: 'gold.vendas', direction: 'up', depth: 1 });
    assert.equal(graph.root, 'gold.vendas');

    connection.close();
    conn.close();
  });
});
