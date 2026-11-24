/**
 * Testes de integração: extensão real num VS Code real, motor fake por ACP.
 */

import assert from 'node:assert/strict';
import * as path from 'node:path';
import * as vscode from 'vscode';
import type { DengcodeApi } from '../../../src/extension';

const EXTENSION_ID = 'dengcode.dengcode';

async function waitFor<T>(what: string, probe: () => T | undefined | false, timeoutMs = 15_000): Promise<T> {
  const start = Date.now();
  for (;;) {
    const value = probe();
    if (value) {
      return value;
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error(`tempo esgotado esperando: ${what}`);
    }
    await new Promise((r) => setTimeout(r, 50));
  }
}

describe('Extensão DengCode (motor fake)', function () {
  let api: DengcodeApi;

  before(async function () {
    const fake = process.env.DENGCODE_FAKE_ENGINE;
    assert.ok(fake, 'DENGCODE_FAKE_ENGINE não definido pelo runTest');
    await vscode.workspace.getConfiguration('dengcode').update('caminhoMotor', fake, vscode.ConfigurationTarget.Global);
    const ext = vscode.extensions.getExtension<DengcodeApi>(EXTENSION_ID);
    assert.ok(ext, 'extensão não encontrada');
    api = await ext.activate();
  });

  after(async function () {
    await api?.controller.stop();
  });

  it('ativa e registra os comandos', async function () {
    const commands = await vscode.commands.getCommands(true);
    for (const id of [
      'dengcode.novaSessao',
      'dengcode.executarSql',
      'dengcode.explicarSql',
      'dengcode.trocarStack',
      'dengcode.trocarWorkspace',
      'dengcode.mostrarLineage',
      'dengcode.cancelar',
    ]) {
      assert.ok(commands.includes(id), `comando ausente: ${id}`);
    }
  });

  it('localiza o motor pela configuração e checa a versão', async function () {
    const engine = await api.engines.resolve();
    assert.ok(engine);
    assert.equal(engine.source, 'setting');
    assert.equal(api.engines.versionOf(engine), 'dengcode 0.1.0');
  });

  it('ida e volta de uma sessão pelo chat', async function () {
    await api.handleWebviewMessage({ type: 'prompt', text: 'olá' });
    assert.match(api.model.agentText(), /Olá! Sou o motor fake do DengCode \(stack local, dev\)\./);
    assert.equal(api.model.isBusy, false);
    assert.ok(api.controller.currentSessionId?.startsWith('fake-'));
    assert.equal(api.controller.activeContext.stack, 'local');
    assert.equal(api.controller.activeContext.workspace, 'default');
  });

  it('diff nativo + aceitar pelo botão do editor', async function () {
    const turn = api.handleWebviewMessage({ type: 'prompt', text: 'editar o modelo silver' });
    const pending = await waitFor('pedido de permissão', () => api.broker.list()[0]);
    assert.equal(pending.toolCallId, 'edit-1');
    const tab = await waitFor('aba de diff', () => api.diffs.openProposalTabs()[0]);
    const input = tab.input as vscode.TabInputTextDiff;
    const proposed = await vscode.workspace.openTextDocument(input.modified);
    assert.match(proposed.getText(), /canal_venda/);
    const original = await vscode.workspace.openTextDocument(input.original);
    assert.doesNotMatch(original.getText(), /canal_venda/);
    await vscode.commands.executeCommand('dengcode.aceitarProposta', input.modified);
    await turn;
    assert.match(api.model.agentText(), /Edição aplicada \(allow_once\)/);
    assert.equal(api.model.getTool('edit-1')?.status, 'completed');
    assert.equal(api.model.getTool('edit-1')?.permission?.status, 'selected');
    await waitFor('abas de diff fechadas', () => api.diffs.openProposalTabs().length === 0);
  });

  it('rejeitar pelo card de aprovação do chat', async function () {
    await api.controller.newSession();
    const turn = api.handleWebviewMessage({ type: 'prompt', text: 'editar de novo' });
    const pending = await waitFor('pedido de permissão', () => api.broker.list()[0]);
    await api.handleWebviewMessage({ type: 'permission', permissionId: pending.permissionId, optionId: 'reject_once' });
    await turn;
    assert.match(api.model.agentText(), /Edição recusada \(reject_once\)/);
    assert.equal(api.model.getTool('edit-1')?.status, 'failed');
  });

  it('SQL: card de aprovação, permissão e prévia de dados', async function () {
    await api.controller.newSession();
    const turn = api.handleWebviewMessage({ type: 'prompt', text: 'rode o sql de pedidos por canal' });
    const pending = await waitFor('pedido de permissão', () => api.broker.list()[0]);
    const tool = api.model.getTool(pending.toolCallId);
    assert.equal(tool?.sql?.kind, 'READ');
    assert.deepEqual(tool?.sql?.tablesRead, ['silver.pedidos']);
    assert.equal(tool?.sql?.cost, 'bytesScanned: 48213 · usd: 0');
    assert.equal(tool?.sql?.warnings.length, 1);
    await api.handleWebviewMessage({ type: 'permission', permissionId: pending.permissionId, optionId: 'allow_once' });
    await turn;
    const done = api.model.getTool('sql-1');
    assert.equal(done?.status, 'completed');
    assert.equal(done?.preview?.rows.length, 3);
    assert.deepEqual(done?.preview?.rows[2], ['NULL', '50']);
    assert.match(api.model.agentText(), /Consulta concluída: 3 linhas\./);
    // A webview do chat desenha todos os itens (card de SQL, plano, prévia) sem erros de JS.
    await vscode.commands.executeCommand('dengcode.chat.focus');
    const expected = api.model.snapshot().items.length;
    await waitFor('chat desenhado na webview', () => api.chat.lastRenderedItems === expected);
    assert.deepEqual(api.chat.webviewErrors, []);
    await api.handleWebviewMessage({ type: 'openPreview', toolCallId: 'sql-1' });
    assert.ok(api.panels.currentPreviewPanel, 'painel de prévia aberto');
    assert.match(api.panels.currentPreviewPanel.webview.html, /canal_venda/);
  });

  it('cancelar o turno', async function () {
    const turn = api.handleWebviewMessage({ type: 'prompt', text: 'tarefa lenta' });
    await waitFor('turno em andamento', () => api.model.agentText().includes('Trabalhando'));
    await vscode.commands.executeCommand('dengcode.cancelar');
    await turn;
    const last = api.model.snapshot().items.at(-1);
    assert.equal(last?.kind === 'notice' ? last.text : '', 'Turno cancelado.');
    assert.equal(api.model.isBusy, false);
  });

  it('lineage pela extensão _dengcode/lineage', async function () {
    const graph = await api.controller.lineage('silver.pedidos', 'down');
    assert.equal(graph.root, 'silver.pedidos');
    assert.equal(graph.edges.length, 2);
    const panel = api.panels.showLineage(graph, 'down');
    assert.match(panel.webview.html, /<svg/);
    assert.match(panel.webview.html, /gold\.vendas_por_canal/);
  });

  it('stack e workspace pelas extensões _dengcode/stack.* e workspace.*', async function () {
    const stacks = await api.controller.stackList();
    assert.deepEqual(
      stacks.stacks.map((s) => s.name),
      ['local', 'opensource'],
    );
    await api.controller.stackUse('opensource', 'prod');
    assert.deepEqual(api.controller.activeContext, { stack: 'opensource', env: 'prod', workspace: 'default' });
    await api.controller.workspaceUse('cliente-a');
    assert.equal(api.controller.activeContext.workspace, 'cliente-a');
  });

  it('CodeLens em .sql e comando "Executar com DengCode"', async function () {
    const folder = vscode.workspace.workspaceFolders?.[0];
    assert.ok(folder, 'workspace de teste aberto');
    const uri = vscode.Uri.file(path.join(folder.uri.fsPath, 'consulta.sql'));
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.window.showTextDocument(doc);
    const lenses = await vscode.commands.executeCommand<vscode.CodeLens[]>('vscode.executeCodeLensProvider', uri);
    const titles = lenses.map((l) => l.command?.title);
    assert.ok(titles.includes('▶ Executar com DengCode'));
    assert.ok(titles.includes('Explicar'));
    await api.controller.newSession();
    const turn = vscode.commands.executeCommand('dengcode.executarSql', uri);
    const pending = await waitFor('permissão do SQL', () => api.broker.list()[0]);
    const user = api.model.snapshot().items.find((i) => i.kind === 'user');
    assert.match(user?.kind === 'user' ? user.text : '', /Execute o SQL abaixo \(o arquivo `consulta\.sql`\)/);
    api.broker.decide(pending.permissionId, 'reject');
    await turn;
    assert.match(api.model.agentText(), /Consulta recusada/);
  });
});
