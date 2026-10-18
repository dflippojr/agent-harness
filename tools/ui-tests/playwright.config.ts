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
  projects: [
    ...(['light', 'dark'] as const).flatMap(colorScheme => [
      { name: 'desktop-' + colorScheme, use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, colorScheme } },
      { name: 'iphone-se-' + colorScheme, use: { ...devices['iPhone SE'], viewport: { width: 375, height: 667 }, colorScheme } },
      { name: 'pixel7-' + colorScheme, use: { ...devices['Pixel 7'], colorScheme } },
    ]),
    // Phones held sideways (#734); only the layout invariants run here. Pixel 7 at its full 915x412 screen gets Stables'
    // wide layout (768 px and up); iPhone SE at 667x375 keeps the phone layout, where #631 and #632 were found.
    { name: 'phone-landscape', use: { ...devices['Pixel 7 landscape'], viewport: { width: 915, height: 412 }, colorScheme: 'light' } },
    { name: 'iphone-se-landscape', use: { ...devices['iPhone SE landscape'], viewport: { width: 667, height: 375 }, colorScheme: 'light' } },
  ],
});
