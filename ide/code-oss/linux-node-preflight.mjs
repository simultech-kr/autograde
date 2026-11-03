// Build-time shell template: this check must run before the bundled Node can
// execute a JavaScript launcher and report a missing Linux shared library.
export function linuxNodePreflight(nodeShellWord) {
  return `# Node 26 official Linux binaries require libatomic in addition to libc.
# Probe in a clean environment so interpreter/linker overrides cannot affect it.
if ! /usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C ${nodeShellWord} --version >/dev/null 2>&1; then
  printf '%s\\n' 'Autograde: bundled Node.js could not start. Check the package architecture and Linux runtime libraries, including libatomic1 (Ubuntu/Debian) or libatomic. Ask the administrator to repair the installation.' >&2
  exit 1
fi
`;
}
