/**
 * Processo real: o cliente fala com o motor fake por stdio (sem VS Code).
 */

import assert from 'node:assert/strict';
import * as path from 'node:path';
import { describe, it } from 'node:test';
import { EngineProcess, runVersion } from '../../src/engine/engineProcess';
import { commandForPath } from '../../src/engine/locator';
import { AcpClient } from '../../src/protocol/acpClient';
import type { DataPreviewParams, SessionNotification, SqlApprovalParams } from '../../src/protocol/acpTypes';
import { checkCompatibility } from '../../src/engine/version';

const FAKE = path.resolve(__dirname, '..', 'fake-engine', 'fake-dengcode.mjs');
const engine = commandForPath(FAKE, 'setting', { nodeExecPath: process.execPath, platform: process.platform });
const logger = { info: (): void => undefined, error: (): void => undefined };

describe('motor fake por stdio', () => {
  it('--version é compatível com a extensão', async () => {
    const out = await runVersion(engine);
    assert.equal(out.trim(), 'dengcode 0.1.0');
    assert.equal(checkCompatibility(out, '0.1.3').compatible, true);
  });

  it('sessão completa: streaming, SQL, permissão, prévia, lineage e stack', async () => {
    const proc = EngineProcess.start(engine, { cwd: process.cwd(), env: { DENGCODE_STACK: 'opensource', DENGCODE_ENV: 'prod' }, logger });
    const client = new AcpClient(proc.connection, { name: 'teste', version: '0.1.0' });
    const updates: SessionNotification[] = [];
    const sql: SqlApprovalParams[] = [];
    const previews: DataPreviewParams[] = [];
    client.setHandlers({
      onSessionUpdate: (n) => updates.push(n),
      onSqlApproval: (p) => sql.push(p),
      onDataPreview: (p) => previews.push(p),
      onRequestPermission: async () => ({ outcome: { outcome: 'selected', optionId: 'allow_once' } }),
    });
    try {
      await client.initialize();
      const stacks = await client.stackList();
      assert.deepEqual(stacks.active, { stack: 'opensource', env: 'prod' }, 'configurações chegam por variáveis de ambiente');
      const { sessionId } = await client.newSession(process.cwd());
      const result = await client.prompt(sessionId, [{ type: 'text', text: 'rode um sql' }]);
      assert.equal(result.stopReason, 'end_turn');
      assert.equal(sql[0]?.toolCallId, 'sql-1');
      assert.equal(previews[0]?.rows.length, 3);
      const kinds = updates.map((u) => u.update.sessionUpdate);
      assert.ok(kinds.includes('plan') && kinds.includes('tool_call') && kinds.includes('tool_call_update'));
      const graph = await client.lineage({ sessionId, target: 'silver.pedidos', direction: 'down', depth: 3 });
      assert.equal(graph.edges.length, 2);
    } finally {
      await proc.stop();
    }
    assert.equal(proc.hasExited, true);
  });

  it('cancelamento devolve stopReason cancelled', async () => {
    const proc = EngineProcess.start(engine, { cwd: process.cwd(), env: {}, logger });
    const client = new AcpClient(proc.connection, { name: 'teste', version: '0.1.0' });
    let resolveStarted!: () => void;
    const started = new Promise<void>((r) => (resolveStarted = r));
    client.setHandlers({ onSessionUpdate: () => resolveStarted() });
    try {
      await client.initialize();
      const { sessionId } = await client.newSession(process.cwd());
      const turn = client.prompt(sessionId, [{ type: 'text', text: 'tarefa lenta' }]);
      await started;
      client.cancel(sessionId);
      assert.equal((await turn).stopReason, 'cancelled');
    } finally {
      await proc.stop();
    }
  });

  it('motor que sai rejeita as requisições pendentes', async () => {
    const broken = commandForPath(path.resolve(__dirname, '..', 'fake-engine', 'nao-existe.mjs'), 'setting', {
      nodeExecPath: process.execPath,
      platform: process.platform,
    });
    const proc = EngineProcess.start(broken, { cwd: process.cwd(), env: {}, logger });
    const client = new AcpClient(proc.connection, { name: 'teste', version: '0.1.0' });
    await assert.rejects(client.initialize(), /encerrou|falha/);
  });
});
