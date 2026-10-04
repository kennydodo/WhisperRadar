export function makeDriver(overrides = {}) {
  const calls = [];
  const driver = {
    calls,
    openProject: async () => {},
    goto: async () => {},
    looksSignedIn: async () => 'in',
    ensureProject: async () => {},
    applyGenerationSettings: async () => {},
    reload: async () => {},
    clearComposerForNextItem: async () => true,
    clearPrompt: async () => {},
    typePrompt: async () => {},
    mentionReferences: async () => {},
    setPrompt: async () => {},
    addReferences: async () => {},
    waitForGridToSettle: async () => ({ entries: [], selector: 'grid' }),
    generate: async () => {},
    waitForNewAssets: async () => ({ added: [], selector: 'grid' }),
    snapshotAssets: async () => ({ entries: [], selector: 'grid' }),
    fetchAssetBytes: async () => null,
    downloadAsset: async () => ({ method: 'export' }),
    convertToPng: async () => false,
    dumpDebug: async () => null,
    ...overrides,
  };

  for (const [name, value] of Object.entries(driver)) {
    if (typeof value !== 'function') continue;
    driver[name] = (...args) => {
      calls.push({ name, args });
      return value(...args);
    };
  }
  return driver;
}

export function callsNamed(driver, name) {
  return driver.calls.filter((call) => call.name === name);
}
