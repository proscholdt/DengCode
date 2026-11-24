import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { ChatModel, diffStats, stringifyFree, type ToolItem } from '../../src/chat/viewModel';
import type { SessionUpdate } from '../../src/protocol/acpTypes';

const chunk = (text: string): SessionUpdate => ({ sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } });

describe('ChatModel', () => {
  it('emenda chunks de texto no mesmo item até outro item aparecer', () => {
    const m = new ChatModel();
    m.addUserMessage('oi');
    m.applyUpdate(chunk('Olá'));
    m.applyUpdate(chunk(', mundo'));
    m.applyUpdate({ sessionUpdate: 'tool_call', toolCallId: 't1', title: 'Lendo', kind: 'read', status: 'in_progress' });
    m.applyUpdate(chunk('Depois'));
    const items = m.snapshot().items;
    assert.deepEqual(
      items.map((i) => i.kind),
      ['user', 'agent', 'tool', 'agent'],
    );
    assert.equal(items[1]?.kind === 'agent' && items[1].text, 'Olá, mundo');
    assert.equal(m.agentText(), 'Olá, mundo\nDepois');
    assert.equal(m.isBusy, true);
  });

  it('pensamento e mensagem ficam em itens separados', () => {
    const m = new ChatModel();
    m.applyUpdate({ sessionUpdate: 'agent_thought_chunk', content: { type: 'text', text: 'hmm' } });
    m.applyUpdate(chunk('resposta'));
    assert.deepEqual(
      m.snapshot().items.map((i) => i.kind),
      ['thought', 'agent'],
    );
  });

  it('tool_call_update mescla campos e substitui conteúdo', () => {
    const m = new ChatModel();
    m.applyUpdate({
      sessionUpdate: 'tool_call',
      toolCallId: 't1',
      title: 'Editar modelo',
      kind: 'edit',
      status: 'pending',
      content: [{ type: 'diff', path: '/p/m.sql', oldText: 'a\nb\n', newText: 'a\nc\nd\n' }],
      locations: [{ path: '/p/m.sql', line: 3 }],
    });
    let tool = m.getTool('t1') as ToolItem;
    assert.deepEqual(tool.content[0], { type: 'diff', path: '/p/m.sql', isNewFile: false, added: 2, removed: 1 });
    assert.deepEqual(tool.locations, ['/p/m.sql:3']);
    const rev = tool.rev;
    m.applyUpdate({ sessionUpdate: 'tool_call_update', toolCallId: 't1', status: 'completed', content: [{ type: 'content', content: { type: 'text', text: 'feito' } }] });
    tool = m.getTool('t1') as ToolItem;
    assert.equal(tool.status, 'completed');
    assert.equal(tool.title, 'Editar modelo');
    assert.deepEqual(tool.content, [{ type: 'text', text: 'feito' }]);
    assert.ok(tool.rev > rev);
  });

  it('update de tool call desconhecida cria o item', () => {
    const m = new ChatModel();
    m.applyUpdate({ sessionUpdate: 'tool_call_update', toolCallId: 'x', status: 'failed' });
    assert.equal(m.getTool('x')?.status, 'failed');
  });

  it('sqlApproval antes do tool_call é guardado e anexado depois', () => {
    const m = new ChatModel();
    m.applySqlApproval({
      sessionId: 'S',
      toolCallId: 't9',
      sql: 'ALTER TABLE t ADD COLUMN c INT',
      kind: 'DDL',
      engine: 'duckdb',
      statements: 1,
      tablesRead: [],
      tablesWritten: ['silver.pedidos'],
      cost: { bytes: 1024, usd: 0 },
      warnings: ['muda o schema'],
      plan: null,
    });
    assert.equal(m.snapshot().items.length, 0);
    m.applyUpdate({ sessionUpdate: 'tool_call', toolCallId: 't9', title: 'query_run', kind: 'execute', status: 'pending' });
    const sql = m.getTool('t9')?.sql;
    assert.equal(sql?.kind, 'DDL');
    assert.deepEqual(sql?.tablesWritten, ['silver.pedidos']);
    assert.equal(sql?.cost, 'bytes: 1024 · usd: 0');
    assert.equal(sql?.plan, undefined);
  });

  it('dataPreview vira grade com no máximo 20 linhas e NULL explícito', () => {
    const m = new ChatModel();
    m.applyUpdate({ sessionUpdate: 'tool_call', toolCallId: 'q', title: 'query_run' });
    m.applyDataPreview({
      sessionId: 'S',
      toolCallId: 'q',
      columns: [
        { name: 'id', type: 'INTEGER' },
        { name: 'email', type: 'VARCHAR' },
      ],
      rows: Array.from({ length: 25 }, (_, i) => ({ id: i, email: i === 0 ? null : '***@***' })),
      totalRows: 300,
      resultPath: '/cache/r.parquet',
    });
    const preview = m.getTool('q')?.preview;
    assert.equal(preview?.rows.length, 20);
    assert.deepEqual(preview?.rows[0], ['0', 'NULL']);
    assert.equal(preview?.totalRows, 300);
    assert.equal(preview?.resultPath, '/cache/r.parquet');
  });

  it('permissão: adiciona, resolve e fim de turno cancela as pendentes', () => {
    const m = new ChatModel();
    const options = [
      { optionId: 'a', name: 'Permitir', kind: 'allow_once' as const },
      { optionId: 'r', name: 'Recusar', kind: 'reject_once' as const },
    ];
    m.addPermission('p1', { toolCallId: 't1', title: 'Escrever', kind: 'edit' }, options);
    m.addPermission('p2', { toolCallId: 't2', title: 'DDL' }, options);
    m.resolvePermission('p1', 'a');
    assert.equal(m.getTool('t1')?.permission?.status, 'selected');
    assert.equal(m.getTool('t1')?.permission?.selectedOptionId, 'a');
    m.endTurn('cancelled');
    assert.equal(m.getTool('t2')?.permission?.status, 'cancelled');
    const last = m.snapshot().items.at(-1);
    assert.equal(last?.kind === 'notice' && last.text, 'Turno cancelado.');
    assert.equal(m.isBusy, false);
  });

  it('plano substitui as entradas no mesmo item', () => {
    const m = new ChatModel();
    m.applyUpdate({ sessionUpdate: 'plan', entries: [{ content: 'a', priority: 'high', status: 'pending' }] });
    m.applyUpdate({
      sessionUpdate: 'plan',
      entries: [
        { content: 'a', priority: 'high', status: 'completed' },
        { content: 'b', priority: 'low', status: 'in_progress' },
      ],
    });
    const plans = m.snapshot().items.filter((i) => i.kind === 'plan');
    assert.equal(plans.length, 1);
    assert.equal(plans[0]?.kind === 'plan' && plans[0].entries.length, 2);
  });

  it('notifica mudanças e reset limpa tudo', () => {
    const m = new ChatModel();
    let calls = 0;
    m.onChange(() => calls++);
    m.addUserMessage('x');
    m.applyUpdate(chunk('y'));
    m.applyUpdate({ sessionUpdate: 'available_commands_update', availableCommands: [] });
    assert.equal(calls, 2);
    m.reset('S2');
    assert.equal(m.snapshot().items.length, 0);
    assert.equal(m.snapshot().sessionId, 'S2');
  });

  it('erro no turno vira aviso de erro', () => {
    const m = new ChatModel();
    m.addUserMessage('x');
    m.failTurn('motor caiu');
    const last = m.snapshot().items.at(-1);
    assert.equal(last?.kind === 'notice' && last.level, 'error');
    assert.equal(m.isBusy, false);
  });
});

describe('auxiliares', () => {
  it('diffStats conta linhas adicionadas e removidas', () => {
    assert.deepEqual(diffStats(null, 'a\nb\n'), { added: 2, removed: 0 });
    assert.deepEqual(diffStats('a\nb', 'a\nb'), { added: 0, removed: 0 });
    assert.deepEqual(diffStats('a\nb\nc', 'a\nc'), { added: 0, removed: 1 });
  });

  it('stringifyFree aceita número, texto e objeto', () => {
    assert.equal(stringifyFree(1.5), 'US$ 1.50');
    assert.equal(stringifyFree(0.001), 'US$ 0.0010');
    assert.equal(stringifyFree('~3 GB'), '~3 GB');
    assert.equal(stringifyFree({ bytesScanned: 10, usd: null }), 'bytesScanned: 10');
    assert.equal(stringifyFree(null), undefined);
  });
});
