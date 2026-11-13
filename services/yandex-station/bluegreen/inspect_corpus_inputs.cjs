const fs = require('fs'), path = require('path'), crypto = require('crypto'), mod = require('module');
const files = {}, sizes = {}, modes = {}, links = {}, packages = new Map(), sourceSeen = new Set();
const allowedRoots = ['/src/', '/node_modules/'];
function sha(file) { return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex'); }
function add(file) {
  const stat = fs.lstatSync(file);
  if (stat.isSymbolicLink()) {
    const target = fs.readlinkSync(file), resolved = fs.realpathSync(file);
    if (path.isAbsolute(target)) throw Error('absolute_symbolic_link');
    if (!allowedRoots.some(root => resolved.startsWith(root))) throw Error('external_symbolic_link');
    links[file] = target;
  } else if (stat.isFile()) {
    files[file] = sha(file); sizes[file] = stat.size; modes[file] = stat.mode & 0o777;
  } else if (stat.isDirectory()) {
    for (const name of fs.readdirSync(file).sort()) add(path.join(file, name));
  } else throw Error('non_regular_export_entry');
}
function visitSource(file) {
  if (sourceSeen.has(file)) return;
  if (!file.startsWith('/src/') || !/\.(ts|js|json)$/.test(file) ||
      /(?:^|\/)(?:ha\.config|ha\.ambiguity|secrets|runtime|history)(?:\.|\/)/.test(file))
    throw Error('source_not_allowlisted');
  sourceSeen.add(file); add(file);
  const text = fs.readFileSync(file, 'utf8');
  for (const match of text.matchAll(/(?:from\s*|import\s*\(|require\s*\()\s*['"]([^'"]+)['"]/g)) {
    const name = match[1];
    if (!name.startsWith('.')) {
      if (!name.startsWith('node:') && !['tslog', 'protobufjs/minimal.js'].includes(name))
        throw Error('unexpected_external_source_dependency');
      continue;
    }
    const base = path.resolve(path.dirname(file), name);
    const next = [base, base + '.ts', base + '.js', base + '.json',
      path.join(base, 'index.ts'), path.join(base, 'index.js')]
      .find(candidate => fs.existsSync(candidate) && fs.statSync(candidate).isFile());
    if (!next) throw Error('relative_source_dependency_missing');
    visitSource(next);
  }
}
function resolvePackage(name, parentFile) {
  const request = mod.createRequire(parentFile);
  try { return fs.realpathSync(request.resolve(name + '/package.json')); } catch {}
  let entry = fs.realpathSync(request.resolve(name));
  while (path.dirname(entry) !== entry) {
    entry = path.dirname(entry);
    const candidate = path.join(entry, 'package.json');
    if (fs.existsSync(candidate) && JSON.parse(fs.readFileSync(candidate, 'utf8')).name === name)
      return candidate;
  }
  throw Error('package_manifest_not_found');
}
function alias(file, realPackage) {
  if (!fs.existsSync(file) || fs.realpathSync(file) !== realPackage)
    throw Error('package_resolution_alias_mismatch');
  if (fs.lstatSync(file).isSymbolicLink()) add(file);
}
function visitPackage(manifestFile) {
  const root = path.dirname(manifestFile);
  if (packages.has(root)) return;
  if (!root.startsWith('/node_modules/')) throw Error('vendor_outside_node_modules');
  const info = JSON.parse(fs.readFileSync(manifestFile, 'utf8'));
  const record = { name: info.name, version: info.version, path: root, dependencies: {}, missing_optional: [] };
  packages.set(root, record);
  add(root);
  const optional = info.optionalDependencies || {};
  const required = { ...(info.dependencies || {}) };
  for (const name of Object.keys(optional)) delete required[name];
  for (const [name, isOptional] of [
    ...Object.keys(required).map(name => [name, false]),
    ...Object.keys(optional).map(name => [name, true])
  ]) {
    let resolved;
    try { resolved = resolvePackage(name, manifestFile); }
    catch (error) { if (isOptional) { record.missing_optional.push(name); continue; } throw error; }
    const depRoot = path.dirname(resolved);
    record.dependencies[name] = depRoot;
    visitPackage(resolved);
    const parentModules = root.slice(0, root.lastIndexOf('/node_modules/') + '/node_modules'.length);
    alias(path.join(parentModules, name), depRoot);
  }
}
visitSource('/src/backend/ha-intents.ts');
for (const name of ['tsx', 'tslog', 'protobufjs']) {
  const manifest = resolvePackage(name, '/package.json');
  visitPackage(manifest);
  alias('/node_modules/' + name, path.dirname(manifest));
}
for (const file of ['/usr/local/bin/node', '/node_modules/.bin/tsx', '/package.json', '/tsconfig.json',
  '/src/backend/corpus/run_corpus.mts', '/src/backend/corpus/run_dialogs.mts',
  '/src/backend/corpus/intents-ru.jsonl']) {
  if (!fs.existsSync(file)) throw Error('required_runtime_or_fixture_missing');
  add(file);
}
const rows = fs.readFileSync('/src/backend/corpus/intents-ru.jsonl', 'utf8').trim().split('\n');
for (const row of rows) JSON.parse(row);
const flags = {};
for (const name of ['NODE_OPTIONS', 'NODE_PATH', 'ESBUILD_BINARY_PATH', 'TSX_TSCONFIG_PATH']) {
  if (process.env[name]) throw Error('unsupported_runtime_override');
}
for (const name of ['AISIS_FAST_EARCON', 'ASR_CORRECT', 'SEMANTIC_INTENTS', 'NODE_ENV']) {
  const value = process.env[name] ?? null;
  if (value !== null && !/^[a-zA-Z0-9_.-]{0,64}$/.test(value)) throw Error('unsupported_nonsensitive_flag');
  flags[name] = value;
}
console.log(JSON.stringify({
  file_sha256: files, file_size: sizes, file_mode: modes, symlinks: links,
  source_files: [...sourceSeen].sort(), packages: [...packages.values()].sort((a,b) => a.path.localeCompare(b.path)),
  corpus_rows: rows.length, runtime: {node: process.version, arch: process.arch, platform: process.platform, flags}
}));