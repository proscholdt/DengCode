#!/usr/bin/env node
// Motor fake do DengCode para os testes da extensão.
//
// Fala ACP (JSON-RPC 2.0 em NDJSON sobre stdio) e roteiriza respostas pelo
// texto do prompt:
//   "editar"  → tool call com diff + pedido de permissão
//   "sql"     → plano + tool call de SQL + _dengcode/sqlApproval + permissão + _dengcode/dataPreview
//   "lento/lenta" → fica trabalhando até receber session/cancel
//   outro     → resposta simples em dois chunks
// Sem dependências: roda com `node` ou com o Electron do VS Code (ELECTRON_RUN_AS_NODE=1).
import * as path from 'node:path';
import * as readline from 'node:readline';

const VERSION = process.env.FAKE_DENGCODE_VERSION ?? '0.1.0';

if (process.argv.includes('--version') || process.argv.includes('-V')) {
  process.stdout.write(`dengcode ${VERSION}\n`);
  process.exit(0);
}
if (!process.argv.includes('acp')) {
  process.stderr.write('uso: fake-dengcode.mjs acp | --version\n');
  process.exit(2);
}

const log = (msg) => process.stderr.write(`[fake] ${msg}\n`);
const send = (msg) => process.stdout.write(`${JSON.stringify({ jsonrpc: '2.0', ...msg })}\n`);
const notify = (method, params) => send({ method, params });

let nextId = 1000;
const pending = new Map();
function request(method, params) {
  const id = nextId++;
  send({ id, method, params });
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}

const state = {
  stack: process.env.DENGCODE_STACK ?? 'local',
  env: process.env.DENGCODE_ENV ?? 'dev',
  workspace: process.env.DENGCODE_WORKSPACE ?? 'default',
  mode: process.env.DENGCODE_MODE ?? 'ask',
  sessions: 0,
  cancelled: new Set(),
  cancelWaiters: new Map(),
};

const update = (sessionId, u) => notify('session/update', { sessionId, update: u });
const say = (sessionId, text) => update(sessionId, { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text } });

const PERMISSION_OPTIONS = [
  { optionId: 'allow_once', name: 'Permitir', kind: 'allow_once' },
  { optionId: 'allow_always', name: 'Sempre nesta sessão', kind: 'allow_always' },
  { optionId: 'reject_once', name: 'Recusar', kind: 'reject_once' },
];

function waitCancel(sessionId) {
  return new Promise((resolve) => state.cancelWaiters.set(sessionId, resolve));
}

async function askPermission(sessionId, toolCall) {
  const result = await request('session/request_permission', { sessionId, toolCall, options: PERMISSION_OPTIONS });
  return result?.outcome ?? { outcome: 'cancelled' };
}

async function scriptEdit(sessionId, cwd) {
  const file = path.join(cwd, 'models', 'silver_pedidos.sql');
  const oldText = 'select id, cliente_id, valor\nfrom bronze.pedidos\n';
  const newText = 'select id, cliente_id, valor, canal_venda\nfrom bronze.pedidos\n';
  say(sessionId, 'Vou adicionar a coluna `canal_venda`.');
  const toolCall = {
    toolCallId: 'edit-1',
    title: `Editar ${path.basename(file)}`,
    kind: 'edit',
    status: 'pending',
    locations: [{ path: file, line: 1 }],
    content: [{ type: 'diff', path: file, oldText, newText }],
  };
  update(sessionId, { sessionUpdate: 'tool_call', ...toolCall });
  const outcome = await askPermission(sessionId, toolCall);
  if (outcome.outcome === 'cancelled') {
    update(sessionId, { sessionUpdate: 'tool_call_update', toolCallId: 'edit-1', status: 'failed' });
    return 'cancelled';
  }
  if (outcome.optionId.startsWith('allow')) {
    update(sessionId, {
      sessionUpdate: 'tool_call_update',
      toolCallId: 'edit-1',
      status: 'completed',
      content: [{ type: 'content', content: { type: 'text', text: 'Arquivo atualizado.' } }],
    });
    say(sessionId, `Edição aplicada (${outcome.optionId}).`);
  } else {
    update(sessionId, { sessionUpdate: 'tool_call_update', toolCallId: 'edit-1', status: 'failed' });
    say(sessionId, `Edição recusada (${outcome.optionId}).`);
  }
  return 'end_turn';
}

async function scriptSql(sessionId) {
  say(sessionId, 'Vou consultar os pedidos.');
  update(sessionId, {
    sessionUpdate: 'plan',
    entries: [
      { content: 'Consultar silver.pedidos', priority: 'high', status: 'in_progress' },
      { content: 'Resumir o resultado', priority: 'medium', status: 'pending' },
    ],
  });
  const toolCallId = 'sql-1';
  const sql = 'SELECT canal_venda, count(*) AS pedidos\nFROM silver.pedidos\nGROUP BY canal_venda';
  update(sessionId, { sessionUpdate: 'tool_call', toolCallId, title: 'query_run', kind: 'execute', status: 'pending', rawInput: { sql } });
  notify('_dengcode/sqlApproval', {
    sessionId,
    toolCallId,
    sql,
    kind: 'READ',
    engine: 'duckdb',
    statements: 1,
    tablesRead: ['silver.pedidos'],
    tablesWritten: [],
    cost: { bytesScanned: 48213, usd: 0 },
    warnings: ['consulta sem LIMIT: o motor injeta LIMIT 1000'],
    plan: 'HASH_GROUP_BY\n  SEQ_SCAN silver.pedidos',
  });
  const outcome = await askPermission(sessionId, { toolCallId, title: 'query_run', kind: 'execute' });
  if (outcome.outcome === 'cancelled') {
    return 'cancelled';
  }
  if (!outcome.optionId.startsWith('allow')) {
    update(sessionId, { sessionUpdate: 'tool_call_update', toolCallId, status: 'failed' });
    say(sessionId, 'Consulta recusada.');
    return 'end_turn';
  }
  update(sessionId, { sessionUpdate: 'tool_call_update', toolCallId, status: 'in_progress' });
  notify('_dengcode/dataPreview', {
    sessionId,
    toolCallId,
    columns: [
      { name: 'canal_venda', type: 'VARCHAR' },
      { name: 'pedidos', type: 'BIGINT' },
    ],
    rows: [
      { canal_venda: 'app', pedidos: 140 },
      { canal_venda: 'site', pedidos: 110 },
      { canal_venda: null, pedidos: 50 },
    ],
    totalRows: 3,
    resultPath: path.join('cache', 'results', 'sql-1.parquet'),
  });
  update(sessionId, {
    sessionUpdate: 'tool_call_update',
    toolCallId,
    status: 'completed',
    content: [{ type: 'content', content: { type: 'text', text: '3 linhas' } }],
  });
  update(sessionId, {
    sessionUpdate: 'plan',
    entries: [
      { content: 'Consultar silver.pedidos', priority: 'high', status: 'completed' },
      { content: 'Resumir o resultado', priority: 'medium', status: 'completed' },
    ],
  });
  say(sessionId, 'Consulta concluída: 3 linhas.');
  return 'end_turn';
}

async function prompt(params) {
  const { sessionId } = params;
  state.cancelled.delete(sessionId);
  const text = params.prompt
    .filter((b) => b.type === 'text')
    .map((b) => b.text)
    .join('\n')
    .toLowerCase();
  let stopReason;
  if (text.includes('editar')) {
    stopReason = await scriptEdit(sessionId, state.cwd ?? process.cwd());
  } else if (text.includes('sql')) {
    stopReason = await scriptSql(sessionId);
  } else if (text.includes('lent')) {
    say(sessionId, 'Trabalhando...');
    await waitCancel(sessionId);
    stopReason = 'cancelled';
  } else {
    say(sessionId, 'Olá! ');
    say(sessionId, `Sou o motor fake do DengCode (stack ${state.stack}, ${state.env}).`);
    stopReason = 'end_turn';
  }
  if (state.cancelled.has(sessionId)) {
    stopReason = 'cancelled';
  }
  return { stopReason };
}

const handlers = {
  initialize: (params) => {
    log(`initialize de ${params?.clientInfo?.name}`);
    return {
      protocolVersion: 1,
      agentCapabilities: {
        loadSession: false,
        promptCapabilities: { image: false, audio: false, embeddedContext: false },
        _meta: { dengcode: { version: 1, extensions: ['sqlApproval', 'dataPreview', 'lineage', 'stack', 'workspace'] } },
      },
      authMethods: [],
      agentInfo: { name: 'dengcode-fake', title: 'DengCode (fake)', version: VERSION },
    };
  },
  'session/new': (params) => {
    state.sessions++;
    state.cwd = params.cwd;
    return {
      sessionId: `fake-${state.sessions}`,
      modes: {
        currentModeId: 'ask',
        availableModes: [
          { id: 'ask', name: 'Perguntar' },
          { id: 'auto', name: 'Automático' },
          { id: 'read_only', name: 'Só leitura' },
        ],
      },
    };
  },
  'session/set_mode': (params) => {
    state.mode = params.modeId;
    return {};
  },
  'session/prompt': prompt,
  '_dengcode/lineage': (params) => ({
    root: params.target,
    nodes: [
      { id: 'bronze.pedidos', kind: 'table' },
      { id: params.target, kind: 'table' },
      { id: 'gold.vendas_por_canal', kind: 'table' },
    ],
    edges:
      params.direction === 'up'
        ? [{ source: 'bronze.pedidos', target: params.target, via: 'silver_pedidos.sql' }]
        : [
            { source: 'bronze.pedidos', target: params.target, via: 'silver_pedidos.sql' },
            { source: params.target, target: 'gold.vendas_por_canal', via: 'gold_vendas.sql' },
          ],
  }),
  '_dengcode/stack.list': () => ({
    stacks: [
      { name: 'local', origin: 'embutida', description: 'Disco + DuckDB + Ollama' },
      { name: 'opensource', origin: 'embutida', description: 'MinIO + Trino + Airflow + Kafka' },
    ],
    active: { stack: state.stack, env: state.env },
  }),
  '_dengcode/stack.use': (params) => {
    state.stack = params.stack;
    state.env = params.env;
    return { stack: state.stack, env: state.env };
  },
  '_dengcode/workspace.list': () => ({ workspaces: ['default', 'cliente-a'], active: state.workspace }),
  '_dengcode/workspace.use': (params) => {
    state.workspace = params.workspace;
    return { workspace: state.workspace };
  },
};

async function handleRequest(msg) {
  const handler = handlers[msg.method];
  if (!handler) {
    send({ id: msg.id, error: { code: -32601, message: `método não encontrado: ${msg.method}` } });
    return;
  }
  try {
    const result = await handler(msg.params ?? {});
    send({ id: msg.id, result: result ?? null });
  } catch (error) {
    send({ id: msg.id, error: { code: -32603, message: String(error?.message ?? error) } });
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  if (!line.trim()) {
    return;
  }
  let msg;
  try {
    msg = JSON.parse(line);
  } catch {
    log(`linha inválida: ${line}`);
    return;
  }
  if (msg.method && msg.id !== undefined) {
    void handleRequest(msg);
  } else if (msg.method) {
    if (msg.method === 'session/cancel') {
      const sessionId = msg.params?.sessionId;
      state.cancelled.add(sessionId);
      state.cancelWaiters.get(sessionId)?.();
      state.cancelWaiters.delete(sessionId);
    }
  } else if (msg.id !== undefined) {
    const p = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) {
      p?.reject(new Error(msg.error.message));
    } else {
      p?.resolve(msg.result);
    }
  }
});
rl.on('close', () => process.exit(0));
log(`pronto (stack ${state.stack}, env ${state.env}, modo ${state.mode})`);
