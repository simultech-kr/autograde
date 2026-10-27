// This overlay is generated from the pinned, pristine Code-OSS sources. It is
// application policy, not protection against modification by the OS account.
const commonPath = 'src/vs/platform/extensionManagement/common/autogradeExtensionPolicy.ts';
const nodePath = 'src/vs/platform/extensionManagement/node/autogradeExtensionPackage.ts';
const header = `/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

`;

const commonSource = header + `import { localize } from '../../../nls.js';
import type { IExtensionDescription, IExtensionManifest } from '../../extensions/common/extensions.js';
import type { IProductConfiguration } from '../../../base/common/product.js';

export const AUTOGRADE_INSTALL_COMMAND = '_autograde.installCourseExtension';
export const AUTOGRADE_INSTALLER_ID = 'autograde.autograde-vscode';

export function autogradeInstallError(): Error {
	return new Error(localize('autograde.managedExtensionsOnly', "확장은 교수자가 관리합니다. ‘수업 확장 설치·업데이트 확인’에서 설치하세요."));
}

export function assertAutogradeDirectInstallAllowed(product: Pick<IProductConfiguration, 'autogradeManagedExtensions'>): void {
	if (product.autogradeManagedExtensions) {
		throw autogradeInstallError();
	}
}

export function assertAutogradeInstaller(extension: IExtensionDescription | undefined): void {
	if (!extension || extension.identifier.value !== AUTOGRADE_INSTALLER_ID || !extension.isBuiltin
		|| extension.isUserBuiltin || extension.isUnderDevelopment || extension.extensionLocation.scheme !== 'file') {
		throw autogradeInstallError();
	}
}

export function assertAutogradeManifest(manifest: IExtensionManifest): void {
	const id = manifest.publisher + '.' + manifest.name;
	if (manifest.extensionDependencies?.length || manifest.extensionPack?.length
		|| id === AUTOGRADE_INSTALLER_ID || id === 'autograde.autograde-ide-profile') {
		throw autogradeInstallError();
	}
}
`;

const nodeSource = header + `import { constants } from 'fs';
import { lstat, open, realpath, mkdtemp, writeFile, rm } from 'fs/promises';
import * as path from 'path';
import { createHash } from 'crypto';
import { autogradeInstallError } from '../common/autogradeExtensionPolicy.js';

const MAX_PACKAGE_BYTES = 64 * 1024 * 1024;

async function directory(candidate: string, privateMode = false): Promise<void> {
	const stat = await lstat(candidate);
	if (!stat.isDirectory() || stat.isSymbolicLink() || (privateMode && process.platform !== 'win32'
		&& (stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0))) {
		throw autogradeInstallError();
	}
}

async function directoryChain(candidate: string): Promise<void> {
	for (let current = candidate; ; current = path.dirname(current)) {
		await directory(current);
		if (path.dirname(current) === current) {
			return;
		}
	}
}

async function readPrivateFile(candidate: string, maximum: number): Promise<Buffer> {
	const before = await lstat(candidate);
	if (!before.isFile() || before.isSymbolicLink()) {
		throw autogradeInstallError();
	}
	const handle = await open(candidate, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
	try {
		const stat = await handle.stat();
		if (!stat.isFile() || stat.dev !== before.dev || stat.ino !== before.ino || stat.nlink !== 1 || stat.size < 1 || stat.size > maximum
			|| (process.platform !== 'win32' && (stat.uid !== process.getuid?.() || (stat.mode & 0o077) !== 0))) {
			throw autogradeInstallError();
		}
		const bytes = Buffer.allocUnsafe(stat.size + 1);
		let count = 0;
		while (count < bytes.length) {
			const result = await handle.read(bytes, count, bytes.length - count, null);
			if (!result.bytesRead) {
				break;
			}
			count += result.bytesRead;
		}
		const after = await handle.stat();
		const entry = await lstat(candidate);
		if (!entry.isFile() || entry.isSymbolicLink() || entry.dev !== stat.dev || entry.ino !== stat.ino
			|| count !== stat.size || after.size !== stat.size || after.mtimeMs !== stat.mtimeMs || after.ctimeMs !== stat.ctimeMs) {
			throw autogradeInstallError();
		}
		return bytes.subarray(0, count);
	} finally {
		await handle.close();
	}
}

/** Validate the launcher's disposable session and snapshot the verified bytes. */
export async function snapshotAutogradeExtension(candidate: string, expectedSha256: string, userDataPath: string, extensionsPath: string,
	environment: NodeJS.ProcessEnv = process.env): Promise<{ path: string; cleanup(): Promise<void> }> {
	const root = environment.AUTOGRADE_SESSION_ROOT;
	const id = environment.AUTOGRADE_SESSION_ID;
	if (environment.AUTOGRADE_LEARNING_LIFETIME !== 'shared' || environment.AUTOGRADE_LEARNING_POLICY !== 'standard'
		|| !root || !path.isAbsolute(root) || path.resolve(root) !== root || root.includes('\\0')
		|| !id || !/^[a-f0-9]{24}$/.test(id) || (path.basename(root) !== 'current' && path.basename(root) !== 'e-' + id)
		|| environment.AUTOGRADE_LEARNER_WORKSPACE !== path.join(root, 'workspace')
		|| environment.AUTOGRADE_LEARNER_MATERIALS !== path.join(root, 'materials')
		|| userDataPath !== path.join(root, 'r', 'user-data') || extensionsPath !== path.join(root, 'r', 'extensions')
		|| typeof expectedSha256 !== 'string' || !/^[a-f0-9]{64}$/.test(expectedSha256)) {
		throw autogradeInstallError();
	}
	const runtime = path.join(root, 'r');
	const staging = path.dirname(candidate);
	if (path.resolve(candidate) !== candidate || path.dirname(staging) !== runtime
		|| !/^extension-install-[A-Za-z0-9]+$/.test(path.basename(staging)) || !candidate.endsWith('.vsix')) {
		throw autogradeInstallError();
	}
	await directoryChain(staging);
	await directory(root, true);
	await directory(runtime, true);
	await directory(staging, true);
	await directory(path.join(root, 'workspace'), true);
	await directory(path.join(root, 'materials'), true);
	if (await realpath(candidate) !== candidate) {
		throw autogradeInstallError();
	}
	const rootStat = await lstat(root);
	const marker = JSON.parse((await readPrivateFile(path.join(root, '.autograde-session.json'), 8192)).toString('utf8')) as {
		schema?: number; sessionId?: string; root?: string; rootDev?: number; rootIno?: number; launcherPid?: number; phase?: string; policy?: string;
	};
	if (!marker || marker.schema !== 1 || marker.policy !== 'standard' || marker.sessionId !== id || marker.root !== root || marker.rootDev !== rootStat.dev
		|| marker.rootIno !== rootStat.ino || !Number.isSafeInteger(marker.launcherPid) || !marker.launcherPid || marker.launcherPid <= 0
		|| (marker.phase !== 'launching' && marker.phase !== 'running')) {
		throw autogradeInstallError();
	}
	process.kill(marker.launcherPid, 0);
	const bytes = await readPrivateFile(candidate, MAX_PACKAGE_BYTES);
	if (createHash('sha256').update(bytes).digest('hex') !== expectedSha256) {
		throw autogradeInstallError();
	}
	await directoryChain(staging);
	const after = await lstat(root);
	if (after.dev !== rootStat.dev || after.ino !== rootStat.ino) {
		throw autogradeInstallError();
	}
	const temporary = await mkdtemp(path.join(runtime, 'course-extension-verified-'));
	const snapshot = path.join(temporary, 'extension.vsix');
	try {
		await writeFile(snapshot, bytes, { flag: 'wx', mode: 0o600 });
		return { path: snapshot, cleanup: () => rm(temporary, { recursive: true, force: true }) };
	} catch (error) {
		await rm(temporary, { recursive: true, force: true });
		throw error;
	}
}
`;

/** Standalone helpers remain executable in tests without an upstream checkout. */
export function policyHelperSources() {
  return new Map([[commonPath, commonSource], [nodePath, nodeSource]]);
}

/** Return all overlay sources without mutating the upstream checkout. */
export function policySources(readUpstream) {
  const sources = policyHelperSources();
  function edit(file, before, after) {
    const source = sources.get(file) ?? String(readUpstream(file));
    const first = source.indexOf(before);
    if (first < 0 || source.indexOf(before, first + before.length) >= 0) {
      throw new Error(`Autograde extension policy anchor must occur exactly once: ${file}: ${before.slice(0, 100)}`);
    }
    sources.set(file, source.slice(0, first) + after + source.slice(first + before.length));
  }
  function importBefore(file, before, addition) { edit(file, before, addition + '\n' + before); }
  function guard(file, signature) {
    edit(file, signature, signature + '\n\t\tassertAutogradeDirectInstallAllowed(this.productService);');
  }
  const platform = 'src/vs/platform/extensionManagement/';
  const workbench = 'src/vs/workbench/services/extensionManagement/common/';
  const api = 'src/vs/workbench/api/';
  const policyImport = "import { assertAutogradeDirectInstallAllowed } from './autogradeExtensionPolicy.js';";

  edit('src/vs/base/common/product.ts', 'export interface IProductConfiguration {', 'export interface IProductConfiguration {\n\treadonly autogradeManagedExtensions?: boolean;');
  edit(platform + 'common/extensionManagement.ts', '\tinstall(vsix: URI, options?: InstallOptions): Promise<ILocalExtension>;', '\tinstall(vsix: URI, options?: InstallOptions): Promise<ILocalExtension>;\n\tinstallAutogradeExtension?(vsix: URI, expectedSha256: string): Promise<ILocalExtension>;');

  const ipc = platform + 'common/extensionManagementIpc.ts';
  edit(ipc, "\t\t\tcase 'installFromLocation': {", "\t\t\tcase 'installAutogradeExtension': {\n\t\t\t\tif (!this.service.installAutogradeExtension) {\n\t\t\t\t\tthrow new Error('Managed course extension installation is unavailable');\n\t\t\t\t}\n\t\t\t\treturn this.service.installAutogradeExtension(transformIncomingURI(args[0], uriTransformer), args[1]);\n\t\t\t}\n\t\t\tcase 'installFromLocation': {");
  edit(ipc, '\tinstallFromLocation(location: URI, profileLocation: URI): Promise<ILocalExtension> {', '\tinstallAutogradeExtension(vsix: URI, expectedSha256: string): Promise<ILocalExtension> {\n\t\treturn this.channel.call<ILocalExtension>(\'installAutogradeExtension\', [vsix, expectedSha256]).then(local => transformIncomingExtension(local, null));\n\t}\n\n\tinstallFromLocation(location: URI, profileLocation: URI): Promise<ILocalExtension> {');

  const native = platform + 'node/extensionManagementService.ts';
  importBefore(native, "import * as fs from 'fs';", "import { assertAutogradeDirectInstallAllowed, assertAutogradeManifest, autogradeInstallError } from '../common/autogradeExtensionPolicy.js';\nimport { snapshotAutogradeExtension } from './autogradeExtensionPackage.js';");
  edit(native, '\tasync install(vsix: URI, options: InstallOptions = {}): Promise<ILocalExtension> {', `\tasync install(vsix: URI, options: InstallOptions = {}): Promise<ILocalExtension> {
		assertAutogradeDirectInstallAllowed(this.productService);
		return this.doInstall(vsix, options);
	}

	async installAutogradeExtension(vsix: URI, expectedSha256: string): Promise<ILocalExtension> {
		if (!this.productService.autogradeManagedExtensions || vsix.scheme !== Schemas.file || vsix.authority || vsix.query || vsix.fragment) {
			throw autogradeInstallError();
		}
		const snapshot = await snapshotAutogradeExtension(vsix.fsPath, expectedSha256, this.environmentService.userDataPath, this.environmentService.extensionsPath);
		try {
			const manifest = await getManifest(snapshot.path);
			assertAutogradeManifest(manifest);
			return await this.doInstall(URI.file(snapshot.path), {
				installGivenVersion: true, donotIncludePackAndDependencies: true, isMachineScoped: true,
				profileLocation: this.userDataProfilesService.defaultProfile.extensionsResource
			});
		} finally {
			await snapshot.cleanup();
		}
	}

	private async doInstall(vsix: URI, options: InstallOptions): Promise<ILocalExtension> {`);
  for (const signature of [
    '\tasync installFromLocation(location: URI, profileLocation: URI): Promise<ILocalExtension> {',
    '\tasync installExtensionsFromProfile(extensions: IExtensionIdentifier[], fromProfileLocation: URI, toProfileLocation: URI): Promise<ILocalExtension[]> {',
    '\tcopyExtensions(fromProfileLocation: URI, toProfileLocation: URI): Promise<void> {',
  ]) guard(native, signature);

  const abstract = platform + 'common/abstractExtensionManagementService.ts';
  importBefore(abstract, "import { distinct, isNonEmptyArray } from '../../../base/common/arrays.js';", policyImport);
  guard(abstract, '\tasync installGalleryExtensions(extensions: InstallExtensionInfo[]): Promise<InstallExtensionResult[]> {');

  const web = workbench + 'webExtensionManagementService.ts';
  importBefore(web, "import { URI } from '../../../../base/common/uri.js';", "import { assertAutogradeDirectInstallAllowed } from '../../../../platform/extensionManagement/common/autogradeExtensionPolicy.js';");
  for (const signature of [
    '\tasync install(location: URI, options: InstallOptions = {}): Promise<ILocalExtension> {',
    '\tasync installExtensionsFromProfile(extensions: IExtensionIdentifier[], fromProfileLocation: URI, toProfileLocation: URI): Promise<ILocalExtension[]> {',
    '\toverride async copyExtensions(fromProfileLocation: URI, toProfileLocation: URI): Promise<void> {',
  ]) guard(web, signature);

  const wb = workbench + 'extensionManagementService.ts';
  importBefore(wb, "import { IProductService } from '../../../../platform/product/common/productService.js';", "import { assertAutogradeDirectInstallAllowed } from '../../../../platform/extensionManagement/common/autogradeExtensionPolicy.js';");
  for (const signature of [
    '\tasync installVSIX(vsix: URI, manifest: IExtensionManifest, options?: InstallOptions): Promise<ILocalExtension> {',
    '\tasync installFromLocation(location: URI): Promise<ILocalExtension> {',
    '\tasync installResourceExtension(extension: IResourceExtension, installOptions: InstallOptions): Promise<ILocalExtension> {',
  ]) guard(wb, signature);

  const protocol = api + 'common/extHost.protocol.ts';
  edit(protocol, 'export interface MainThreadCommandsShape extends IDisposable {', 'export interface MainThreadCommandsShape extends IDisposable {\n\t$installAutogradeExtension(vsix: UriComponents, expectedSha256: string, callerId: string): Promise<void>;');

  const extApi = api + 'common/extHost.api.impl.ts';
  importBefore(extApi, "import type * as vscode from 'vscode';", "import { AUTOGRADE_INSTALL_COMMAND, assertAutogradeInstaller, autogradeInstallError } from '../../../platform/extensionManagement/common/autogradeExtensionPolicy.js';");
  edit(extApi, '\t\t\texecuteCommand<T>(id: string, ...args: unknown[]): Thenable<T> {\n\t\t\t\treturn extHostCommands.executeCommand<T>(id, ...args);', `\t\t\texecuteCommand<T>(id: string, ...args: unknown[]): Thenable<T> {
				if (id === AUTOGRADE_INSTALL_COMMAND) {
					assertAutogradeInstaller(extension);
					const [vsix, expectedSha256] = args;
					if (args.length !== 2 || !URI.isUri(vsix) || typeof expectedSha256 !== 'string') {
						throw autogradeInstallError();
					}
					return rpcProtocol.getProxy(MainContext.MainThreadCommands).$installAutogradeExtension(vsix, expectedSha256, extension.identifier.value).then(() => undefined as T);
				}
				return extHostCommands.executeCommand<T>(id, ...args);`);

  const commands = api + 'common/extHostCommands.ts';
  importBefore(commands, "import { validateConstraint } from '../../../base/common/types.js';", "import { AUTOGRADE_INSTALL_COMMAND, autogradeInstallError } from '../../../platform/extensionManagement/common/autogradeExtensionPolicy.js';");
  edit(commands, "\t\tthis._logService.trace('ExtHostCommands#registerCommand', id);", "\t\tif (id === AUTOGRADE_INSTALL_COMMAND) {\n\t\t\tthrow autogradeInstallError();\n\t\t}\n\t\tthis._logService.trace('ExtHostCommands#registerCommand', id);");
  edit(commands, "\t\tthis._logService.trace('ExtHostCommands#executeCommand', id);", "\t\tif (id === AUTOGRADE_INSTALL_COMMAND) {\n\t\t\tthrow autogradeInstallError();\n\t\t}\n\t\tthis._logService.trace('ExtHostCommands#executeCommand', id);");

  const main = api + 'browser/mainThreadCommands.ts';
  importBefore(main, "import { isString } from '../../../base/common/types.js';", `import { AUTOGRADE_INSTALL_COMMAND, AUTOGRADE_INSTALLER_ID, assertAutogradeInstaller, autogradeInstallError } from '../../../platform/extensionManagement/common/autogradeExtensionPolicy.js';
import { URI, UriComponents } from '../../../base/common/uri.js';
import { IProductService } from '../../../platform/product/common/productService.js';
import { IExtensionManagementServerService } from '../../services/extensionManagement/common/extensionManagement.js';
import { ExtensionHostKind } from '../../services/extensions/common/extensionHostKind.js';`);
  edit(main, '\t\textHostContext: IExtHostContext,', '\t\tprivate readonly extHostContext: IExtHostContext,');
  edit(main, '\t\t@IExtensionService private readonly _extensionService: IExtensionService,', '\t\t@IExtensionService private readonly _extensionService: IExtensionService,\n\t\t@IProductService private readonly productService: IProductService,\n\t\t@IExtensionManagementServerService private readonly extensionManagementServerService: IExtensionManagementServerService,');
  edit(main, '\t$registerCommand(id: string): void {', '\t$registerCommand(id: string): void {\n\t\tif (id === AUTOGRADE_INSTALL_COMMAND) {\n\t\t\tthrow autogradeInstallError();\n\t\t}');
  edit(main, '\tasync $executeCommand<T>(id: string, args: unknown[] | SerializableObjectWithBuffers<unknown[]>, retry: boolean): Promise<T | undefined> {', '\tasync $executeCommand<T>(id: string, args: unknown[] | SerializableObjectWithBuffers<unknown[]>, retry: boolean): Promise<T | undefined> {\n\t\tif (id === AUTOGRADE_INSTALL_COMMAND) {\n\t\t\tthrow autogradeInstallError();\n\t\t}');
  edit(main, '\t$getCommands(): Promise<string[]> {', `\tasync $installAutogradeExtension(vsix: UriComponents, expectedSha256: string, callerId: string): Promise<void> {
		if (!this.productService.autogradeManagedExtensions || callerId !== AUTOGRADE_INSTALLER_ID
			|| this.extHostContext.remoteAuthority || this.extHostContext.extensionHostKind !== ExtensionHostKind.LocalProcess
			|| this.extensionManagementServerService.remoteExtensionManagementServer) {
			throw autogradeInstallError();
		}
		assertAutogradeInstaller(await this._extensionService.getExtension(callerId));
		const service = this.extensionManagementServerService.localExtensionManagementServer?.extensionManagementService;
		if (!service?.installAutogradeExtension) {
			throw autogradeInstallError();
		}
		await service.installAutogradeExtension(URI.revive(vsix), expectedSha256);
	}

	$getCommands(): Promise<string[]> {`);

  // Preserve the upstream command tests after adding injected policy services.
  const mainTest = api + 'test/browser/mainThreadCommands.test.ts';
  importBefore(mainTest, "import assert from 'assert';", "import { IProductService } from '../../../../platform/product/common/productService.js';\nimport { IExtensionManagementServerService } from '../../../services/extensionManagement/common/extensionManagement.js';");
  for (const name of ['dispose on unregister', 'unregister all on dispose']) {
    const before = `\ttest('${name}', function () {\n\n\t\tconst commands = new MainThreadCommands(SingleProxyRPCProtocol(null), undefined!, new class extends mock<IExtensionService>() { });`;
    edit(mainTest, before, before.replace('mock<IExtensionService>() { });', 'mock<IExtensionService>() { }, new class extends mock<IProductService>() { }, new class extends mock<IExtensionManagementServerService>() { });'));
  }
  edit(mainTest, "\t\t\t\t\tactivations.push(id);\n\t\t\t\t\treturn Promise.resolve();\n\t\t\t\t}\n\t\t\t}\n\t\t);", "\t\t\t\t\tactivations.push(id);\n\t\t\t\t\treturn Promise.resolve();\n\t\t\t\t}\n\t\t\t},\n\t\t\tnew class extends mock<IProductService>() { },\n\t\t\tnew class extends mock<IExtensionManagementServerService>() { }\n\t\t);");

  return sources;
}
