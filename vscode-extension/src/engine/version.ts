/**
 * Versões do motor e compatibilidade com a extensão.
 *
 * Regra: motor e extensão são compatíveis quando `major.minor` coincidem
 * (o patch pode variar). A extensão é versionada junto com o motor.
 */

export interface SemVer {
  major: number;
  minor: number;
  patch: number;
  prerelease?: string;
}

const SEMVER = /(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?/;

/** Extrai a primeira versão `x.y.z` de um texto (ex.: a saída de `dengcode --version`). */
export function parseVersion(text: string): SemVer | undefined {
  const match = SEMVER.exec(text);
  if (!match) {
    return undefined;
  }
  const version: SemVer = { major: Number(match[1]), minor: Number(match[2]), patch: Number(match[3]) };
  if (match[4]) {
    version.prerelease = match[4];
  }
  return version;
}

export function formatVersion(version: SemVer): string {
  return `${version.major}.${version.minor}.${version.patch}${version.prerelease ? `-${version.prerelease}` : ''}`;
}

export type Compatibility =
  | { compatible: true; engine: SemVer }
  | { compatible: false; reason: 'unparseable'; output: string }
  | { compatible: false; reason: 'mismatch'; engine: SemVer; expected: SemVer };

/** Compara a saída de `dengcode --version` com a versão da extensão. */
export function checkCompatibility(versionOutput: string, extensionVersion: string): Compatibility {
  const engine = parseVersion(versionOutput);
  const expected = parseVersion(extensionVersion);
  if (!engine || !expected) {
    return { compatible: false, reason: 'unparseable', output: versionOutput.trim() };
  }
  if (engine.major === expected.major && engine.minor === expected.minor) {
    return { compatible: true, engine };
  }
  return { compatible: false, reason: 'mismatch', engine, expected };
}

/** Mensagem em português para o usuário. */
export function describeCompatibility(result: Compatibility, extensionVersion: string): string {
  if (result.compatible) {
    return `motor ${formatVersion(result.engine)} compatível com a extensão ${extensionVersion}`;
  }
  if (result.reason === 'unparseable') {
    return `não foi possível ler a versão do motor (saída: "${result.output.slice(0, 120)}")`;
  }
  return (
    `motor ${formatVersion(result.engine)} incompatível com a extensão ${extensionVersion}: ` +
    `é preciso um motor ${result.expected.major}.${result.expected.minor}.x`
  );
}
