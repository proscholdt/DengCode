/**
 * Seleção do binário do motor publicado nos GitHub Releases.
 *
 * Padrão: `https://github.com/proscholdt/DengCode/releases/download/v<versão>/dengcode-<so>-<arq>[.exe]`.
 * Os nomes de alvo seguem a fase 8 (win-x64, linux-x64, linux-arm64,
 * macos-x64, macos-arm64). As releases só existem a partir da fase 8.
 */

export const RELEASE_BASE_URL = 'https://github.com/proscholdt/DengCode/releases/download';

const OS_NAMES: Partial<Record<string, string>> = {
  win32: 'win',
  linux: 'linux',
  darwin: 'macos',
};

const ARCH_NAMES: Partial<Record<string, string>> = {
  x64: 'x64',
  arm64: 'arm64',
};

/** Alvos para os quais a fase 8 publica binários. */
export const SUPPORTED_TARGETS: readonly string[] = ['win-x64', 'linux-x64', 'linux-arm64', 'macos-x64', 'macos-arm64'];

export interface ReleaseAsset {
  target: string;
  fileName: string;
  url: string;
}

/** `process.platform`/`process.arch` → alvo (`win-x64`...) ou `undefined` se não houver binário. */
export function platformTarget(platform: string, arch: string): string | undefined {
  const os = OS_NAMES[platform];
  const cpu = ARCH_NAMES[arch];
  if (!os || !cpu) {
    return undefined;
  }
  const target = `${os}-${cpu}`;
  return SUPPORTED_TARGETS.includes(target) ? target : undefined;
}

export function assetFileName(platform: string, arch: string): string | undefined {
  const target = platformTarget(platform, arch);
  if (!target) {
    return undefined;
  }
  return `dengcode-${target}${platform === 'win32' ? '.exe' : ''}`;
}

/** URL do binário para a versão da extensão. A versão aceita com ou sem `v`. */
export function releaseAsset(version: string, platform: string, arch: string): ReleaseAsset | undefined {
  const fileName = assetFileName(platform, arch);
  const target = platformTarget(platform, arch);
  if (!fileName || !target) {
    return undefined;
  }
  const tag = version.startsWith('v') ? version : `v${version}`;
  return { target, fileName, url: `${RELEASE_BASE_URL}/${tag}/${fileName}` };
}
