import { defineConfig, devices } from '@playwright/test';
export default defineConfig({
  testDir: './tests', fullyParallel: false, workers: 1, retries: 0,
  timeout: 60_000, expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  globalTeardown: './teardown.mjs',
  use: { baseURL: 'http://127.0.0.1:18801', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: 'node start.mjs', url: 'http://127.0.0.1:18801/ready',
    reuseExistingServer: false, timeout: 180_000, stdout: 'ignore', stderr: 'pipe',
  },
  projects: (['light', 'dark'] as const).flatMap(colorScheme => [
    { name: 'desktop-' + colorScheme, use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, colorScheme } },
    { name: 'iphone-se-' + colorScheme, use: { ...devices['iPhone SE'], viewport: { width: 375, height: 667 }, colorScheme } },
    { name: 'pixel7-' + colorScheme, use: { ...devices['Pixel 7'], colorScheme } },
  ]),
});
