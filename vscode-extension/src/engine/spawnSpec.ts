/**
 * Monta a chamada de processo do motor (comando, argumentos, ambiente).
 * Puro, para ser testado sem abrir processos.
 */

import type { EngineCommand } from './locator';

export type PermissionMode = 'ask' | 'auto' | 'read_only';

export interface EngineSettings {
  stack: string;
  env: string;
  permissionMode: PermissionMode;
  workspace: string;
}

/**
 * Variáveis de ambiente que levam as configurações da extensão ao motor.
 * Só são definidas quando têm valor; o motor usa os próprios padrões no resto.
 */
export function settingsEnv(settings: EngineSettings): Record<string, string> {
  const env: Record<string, string> = {};
  if (settings.stack.trim()) {
    env.DENGCODE_STACK = settings.stack.trim();
  }
  if (settings.env.trim()) {
    env.DENGCODE_ENV = settings.env.trim();
  }
  if (settings.permissionMode) {
    env.DENGCODE_MODE = settings.permissionMode;
  }
  if (settings.workspace.trim()) {
    env.DENGCODE_WORKSPACE = settings.workspace.trim();
  }
  return env;
}

export interface SpawnSpec {
  command: string;
  args: string[];
  shell: boolean;
  env: Record<string, string | undefined>;
}

/** Aspas para o `cmd.exe` quando o motor é um `.cmd`/`.bat` (só rodam via shell). */
export function quoteForCmd(arg: string): string {
  if (/^[\w\-.:\\/=]+$/.test(arg)) {
    return arg;
  }
  return `"${arg.replace(/"/g, '""')}"`;
}

export function buildSpawnSpec(
  engine: EngineCommand,
  subcommand: string[],
  baseEnv: Record<string, string | undefined>,
  extraEnv: Record<string, string> = {},
): SpawnSpec {
  const args = [...engine.args, ...subcommand];
  const env = {
    ...baseEnv,
    ...engine.env,
    ...extraEnv,
    // Saída do motor sempre em UTF-8, inclusive no Windows.
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
  };
  if (engine.shell) {
    return { command: [engine.command, ...args].map(quoteForCmd).join(' '), args: [], shell: true, env };
  }
  return { command: engine.command, args, shell: false, env };
}
