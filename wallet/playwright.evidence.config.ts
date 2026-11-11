// Copied next to the app's playwright.config.ts in a throwaway clone by e2e.py.
// Same suite, but always keep evidence and fail fast so base/head runs are comparable.
import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  retries: 0,
  reporter: [['json', { outputFile: 'e2e-results.json' }], ['list']],
  use: { ...base.use, screenshot: 'on', video: 'on', trace: 'on' },
  projects: (base.projects ?? []).filter((p) => p.name === 'Chromium'),
});
