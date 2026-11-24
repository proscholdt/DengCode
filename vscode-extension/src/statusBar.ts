/**
 * Barra de status: stack · ambiente e workspace (clique troca).
 */

import * as vscode from 'vscode';
import type { ActiveContext } from './session/sessionController';

export function statusTexts(active: ActiveContext): { stack: string; workspace: string } {
  const stack = active.stack ?? 'stack padrão';
  const env = active.env ?? 'dev';
  return { stack: `$(layers) ${stack} · ${env}`, workspace: `$(folder-library) ${active.workspace ?? 'workspace ativo'}` };
}

export class DengcodeStatusBar implements vscode.Disposable {
  private readonly stackItem: vscode.StatusBarItem;
  private readonly workspaceItem: vscode.StatusBarItem;

  constructor() {
    this.stackItem = vscode.window.createStatusBarItem('dengcode.stack', vscode.StatusBarAlignment.Left, 50);
    this.stackItem.name = 'DengCode: stack e ambiente';
    this.stackItem.command = 'dengcode.trocarStack';
    this.workspaceItem = vscode.window.createStatusBarItem('dengcode.workspace', vscode.StatusBarAlignment.Left, 49);
    this.workspaceItem.name = 'DengCode: workspace';
    this.workspaceItem.command = 'dengcode.trocarWorkspace';
  }

  update(active: ActiveContext): void {
    const texts = statusTexts(active);
    this.stackItem.text = texts.stack;
    this.stackItem.tooltip = `DengCode — stack ${active.stack ?? '(padrão do motor)'}, ambiente ${active.env ?? 'dev'}. Clique para trocar.`;
    this.workspaceItem.text = texts.workspace;
    this.workspaceItem.tooltip = `DengCode — workspace ${active.workspace ?? '(ativo no motor)'}. Clique para trocar.`;
    if (active.env === 'prod') {
      this.stackItem.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
    } else {
      this.stackItem.backgroundColor = undefined;
    }
    this.stackItem.show();
    this.workspaceItem.show();
  }

  dispose(): void {
    this.stackItem.dispose();
    this.workspaceItem.dispose();
  }
}
