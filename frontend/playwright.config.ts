import { defineConfig, devices } from "@playwright/test";

const BASE_URL = process.env.PLAYWRIGHT_BASE_URL ?? "http://localhost:8080";

// ECS ALB is in eu-west-1 — give extra time for network round-trips
const IS_ECS = BASE_URL.includes("amazonaws") || BASE_URL.includes("elb.amazonaws");

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [["line"], ["html", { outputFolder: "playwright-report", open: "never" }]]
    : [["html", { open: "on-failure" }]],
  // Per-test timeout (longer for ECS to account for EU round-trips)
  timeout: IS_ECS ? 90_000 : 30_000,
  // Default expect() assertion timeout
  expect: { timeout: IS_ECS ? 30_000 : 5_000 },
  use: {
    baseURL: BASE_URL,
    // Allow self-signed / mismatched certs on ECS ALB (no custom domain)
    ignoreHTTPSErrors: true,
    // Per-action timeout (click, fill, etc.)
    actionTimeout: IS_ECS ? 30_000 : 10_000,
    // page.goto / page.waitForNavigation timeout
    navigationTimeout: IS_ECS ? 60_000 : 30_000,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
