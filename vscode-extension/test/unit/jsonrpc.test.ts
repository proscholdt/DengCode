import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import {
  ConnectionClosedError,
  JsonRpcConnection,
  JsonRpcError,
  LineDecoder,
  classifyMessage,
  encodeMessage,
} from '../../src/protocol/jsonrpc';

function harness(): { conn: JsonRpcConnection; sent: unknown[]; logs: string[] } {
  const sent: unknown[] = [];
  const logs: string[] = [];
  const conn = new JsonRpcConnection(
    (line) => {
      assert.ok(line.endsWith('\n'), 'cada mensagem termina com \\n');
      assert.equal(line.indexOf('\n'), line.length - 1, 'uma mensagem por linha');
      sent.push(JSON.parse(line));
    },
    (m) => logs.push(m),
  );
  return { conn, sent, logs };
}

const tick = (): Promise<void> => new Promise((r) => setImmediate(r));

describe('LineDecoder', () => {
  it('junta pedaços e separa linhas', () => {
    const d = new LineDecoder();
    assert.deepEqual(d.push('{"a":'), []);
    assert.deepEqual(d.push('1}\n{"b":2}\n{"c"'), ['{"a":1}', '{"b":2}']);
    assert.deepEqual(d.push(':3}\n'), ['{"c":3}']);
  });

  it('aceita CRLF e ignora linhas vazias', () => {
    const d = new LineDecoder();
    assert.deepEqual(d.push('{"a":1}\r\n\r\n  \n{"b":2}\r\n'), ['{"a":1}', '{"b":2}']);
  });

  it('preserva UTF-8 quebrado entre pedaços', () => {
    const bytes = Buffer.from('{"t":"ação ✓"}\n', 'utf8');
    const d = new LineDecoder();
    const out: string[] = [];
    for (const byte of bytes) {
      out.push(...d.push(Uint8Array.of(byte)));
    }
    assert.deepEqual(out, ['{"t":"ação ✓"}']);
  });

  it('flush devolve o resto sem \\n', () => {
    const d = new LineDecoder();
    d.push('{"x":1}');
    assert.deepEqual(d.flush(), ['{"x":1}']);
    assert.deepEqual(d.flush(), []);
  });

  it('rejeita linha gigante', () => {
    const d = new LineDecoder(10);
    assert.throws(() => d.push('x'.repeat(20)));
  });
});

describe('classifyMessage / encodeMessage', () => {
  it('classifica requisição, notificação e resposta', () => {
    assert.equal(classifyMessage({ jsonrpc: '2.0', id: 1, method: 'm' })?.kind, 'request');
    assert.equal(classifyMessage({ jsonrpc: '2.0', id: 'a', method: 'm' })?.kind, 'request');
    assert.equal(classifyMessage({ jsonrpc: '2.0', method: 'm' })?.kind, 'notification');
    assert.equal(classifyMessage({ jsonrpc: '2.0', id: 1, result: null })?.kind, 'response');
    assert.equal(classifyMessage({ jsonrpc: '2.0', id: null, error: { code: 1, message: 'x' } })?.kind, 'response');
  });

  it('rejeita o que não é JSON-RPC 2.0', () => {
    assert.equal(classifyMessage(null), undefined);
    assert.equal(classifyMessage([]), undefined);
    assert.equal(classifyMessage({ id: 1, method: 'm' }), undefined);
    assert.equal(classifyMessage({ jsonrpc: '2.0', id: 1 }), undefined);
  });

  it('escapa quebras de linha dentro de strings', () => {
    const line = encodeMessage({ jsonrpc: '2.0', method: 'm', params: { t: 'a\nb' } });
    assert.equal(line.split('\n').length, 2);
  });
});

describe('JsonRpcConnection', () => {
  it('casa resposta com a requisição pelo id', async () => {
    const { conn, sent } = harness();
    const p1 = conn.request('um', { x: 1 });
    const p2 = conn.request('dois');
    assert.deepEqual(sent[0], { jsonrpc: '2.0', id: 1, method: 'um', params: { x: 1 } });
    assert.deepEqual(sent[1], { jsonrpc: '2.0', id: 2, method: 'dois' });
    conn.receive('{"jsonrpc":"2.0","id":2,"result":"b"}\n{"jsonrpc":"2.0","id":1,"result":"a"}\n');
    assert.equal(await p1, 'a');
    assert.equal(await p2, 'b');
  });

  it('propaga erro do outro lado como JsonRpcError', async () => {
    const { conn } = harness();
    const p = conn.request('falha');
    conn.receive('{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"params inválidos","data":{"campo":"x"}}}\n');
    await assert.rejects(p, (e: unknown) => e instanceof JsonRpcError && e.code === -32602 && e.message === 'params inválidos');
  });

  it('responde requisições recebidas com o resultado do handler', async () => {
    const { conn, sent } = harness();
    conn.onRequest(async (method, params) => ({ method, params }));
    conn.receive('{"jsonrpc":"2.0","id":"r1","method":"eco","params":{"v":1}}\n');
    await tick();
    assert.deepEqual(sent[0], { jsonrpc: '2.0', id: 'r1', result: { method: 'eco', params: { v: 1 } } });
  });

  it('erro no handler vira resposta de erro', async () => {
    const { conn, sent } = harness();
    conn.onRequest(async () => {
      throw new JsonRpcError(-32601, 'não suportado');
    });
    conn.receive('{"jsonrpc":"2.0","id":7,"method":"fs/read_text_file"}\n');
    await tick();
    assert.deepEqual(sent[0], { jsonrpc: '2.0', id: 7, error: { code: -32601, message: 'não suportado' } });
  });

  it('sem handler, responde method not found', async () => {
    const { conn, sent } = harness();
    conn.receive('{"jsonrpc":"2.0","id":3,"method":"x"}\n');
    await tick();
    assert.equal((sent[0] as { error: { code: number } }).error.code, -32601);
  });

  it('despacha notificações', () => {
    const { conn } = harness();
    const got: Array<[string, unknown]> = [];
    conn.onNotification((m, p) => got.push([m, p]));
    conn.receive('{"jsonrpc":"2.0","method":"session/update","params":{"a":1}}\n');
    assert.deepEqual(got, [['session/update', { a: 1 }]]);
  });

  it('ignora linhas que não são JSON (log no stdout)', () => {
    const { conn, logs } = harness();
    conn.receive('INFO iniciando\n');
    assert.ok(logs.some((l) => l.includes('não é JSON')));
  });

  it('fechar rejeita as pendentes e recusa novas', async () => {
    const { conn } = harness();
    const p = conn.request('longa');
    let closedWith: Error | undefined;
    conn.onClose((e) => (closedWith = e));
    conn.end(new ConnectionClosedError('motor saiu'));
    await assert.rejects(p, /motor saiu/);
    await assert.rejects(conn.request('outra'), /motor saiu/);
    assert.equal(closedWith?.message, 'motor saiu');
    assert.equal(conn.isClosed, true);
  });

  it('timeout e abort', async () => {
    const { conn } = harness();
    await assert.rejects(conn.request('lenta', undefined, { timeoutMs: 10 }), /tempo esgotado/);
    const controller = new AbortController();
    const p = conn.request('cancelável', undefined, { signal: controller.signal });
    controller.abort();
    await assert.rejects(p, /cancelada/);
  });

  it('resposta nula vira null', async () => {
    const { conn } = harness();
    const p = conn.request('vazia');
    conn.receive('{"jsonrpc":"2.0","id":1,"result":null}\n');
    assert.equal(await p, null);
  });
});
