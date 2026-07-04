// Metro config for the Splitr monorepo (mobile/ + packages/core/).
// Standard Expo monorepo setup: https://docs.expo.dev/guides/monorepos/
const { getDefaultConfig } = require('expo/metro-config');
const path = require('path');

const projectRoot = __dirname;
const workspaceRoot = path.resolve(projectRoot, '..');

const config = getDefaultConfig(projectRoot);

// Watch the whole workspace (packages/core) for changes, not just mobile/.
config.watchFolders = [workspaceRoot];

// Resolve modules from mobile/node_modules first, then the hoisted root
// node_modules (npm workspaces hoists @splitr/core's deps like zod there).
config.resolver.nodeModulesPaths = [
  path.resolve(projectRoot, 'node_modules'),
  path.resolve(workspaceRoot, 'node_modules'),
];
config.resolver.disableHierarchicalLookup = true;

module.exports = config;
