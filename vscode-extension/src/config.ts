/**
 * Leitura das configurações `dengcode.*`.
 */

import * as vscode from 'vscode';
import type { EngineSettings, PermissionMode } from './engine/spawnSpec';

export interface DengcodeConfig extends EngineSettings {
  enginePath: string;
}

const MODES: PermissionMode[] = ['ask', 'auto', 'read_only'];

export function readConfig(): DengcodeConfig {
  const cfg = vscode.workspace.getConfiguration('dengcode');
  const mode = cfg.get<string>('modoPermissao', 'ask');
  return {
    enginePath: cfg.get<string>('caminhoMotor', '') ?? '',
    stack: cfg.get<string>('stackPadrao', '') ?? '',
    env: cfg.get<string>('ambiente', 'dev') ?? 'dev',
    permissionMode: (MODES as string[]).includes(mode) ? (mode as PermissionMode) : 'ask',
    workspace: cfg.get<string>('workspace', '') ?? '',
  };
}

/** Pasta onde o motor roda: a primeira pasta do workspace (ou a do arquivo ativo). */
export function projectFolder(): string | undefined {
  const active = vscode.window.activeTextEditor?.document.uri;
  if (active) {
    const folder = vscode.workspace.getWorkspaceFolder(active);
    if (folder) {
      return folder.uri.fsPath;
    }
  }
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}
