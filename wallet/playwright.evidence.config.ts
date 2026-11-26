// Copied next to the app's playwright.config.ts in a throwaway clone by e2e.py.
// Same suite, keeping evidence on every test and retrying transient timing flakes.
import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  retries: 2,
  reporter: [['json', { outputFile: 'e2e-results.json' }], ['list']],
  use: { ...base.use, screenshot: 'on', video: 'on', trace: 'on' },
  projects: (base.projects ?? []).filter((p) => p.name === 'Chromium'),
});
