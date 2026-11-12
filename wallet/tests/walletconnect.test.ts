// SPDX-License-Identifier: Apache-2.0
// Copied into the app clone's frontends/web/tests/ by e2e.py wc; imports resolve against the clone.
//
// A WalletConnect dapp (wc/dapp.mjs) pairs with the app over the public relay and sends signing
// requests. The app approves them and the graphical simulator (wc/device.py) confirms on device.
// Evidence goes to $WC_OUT: app/*.png + app/steps.json, device/*.png + device/frames.json.
//
// Env: WC_OUT, WC_DAPP (dapp.mjs), WC_DEVICE (device.py), SIMULATOR_DIR (has simulator-graphical),
// E2E_DISPLAY (Xvfb display for the simulator).

import { expect, type Page } from '@playwright/test';
import { spawn, type ChildProcess } from 'child_process';
import { createInterface } from 'readline';
import * as fs from 'fs';
import * as path from 'path';
import { test } from './helpers/fixtures';
import { ServeWallet } from './helpers/servewallet';
import { deleteAccountsFile, deleteConfigFile } from './helpers/fs';

type TEvent = { event: string; [key: string]: unknown };

// A child process speaking one JSON object per line on stdout; stdin takes commands.
class LineProc {
  private readonly proc: ChildProcess;
  private readonly events: TEvent[] = [];
  private waiters: Array<() => void> = [];
  private exited = false;

  constructor(name: string, cmd: string, args: string[], logPath: string) {
    const log = fs.createWriteStream(logPath);
    this.proc = spawn(cmd, args, { stdio: ['pipe', 'pipe', 'pipe'], detached: true });
    this.proc.stderr!.pipe(log);
    // Writing to a process that already exited must not crash the test.
    this.proc.stdin!.on('error', () => undefined);
    createInterface({ input: this.proc.stdout! }).on('line', line => {
      log.write(`[stdout] ${line}\n`);
      try {
        this.events.push(JSON.parse(line) as TEvent);
      } catch {
        return;
      }
      this.wake();
    });
    this.proc.on('exit', code => {
      log.write(`[${name}] exited ${String(code)}\n`);
      this.exited = true;
      this.wake();
    });
  }

  private wake() {
    const w = this.waiters;
    this.waiters = [];
    w.forEach(f => f());
  }

  send(line: string) {
    this.proc.stdin!.write(line + '\n');
  }

  // Consumes and returns the first event of the given type.
  async next(event: string, timeoutMs: number): Promise<TEvent> {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const i = this.events.findIndex(e => e.event === event || e.event === 'error');
      if (i >= 0) {
        const [e] = this.events.splice(i, 1);
        if (e!.event === 'error') {
          throw new Error(`${event}: ${JSON.stringify(e)}`);
        }
        return e!;
      }
      if (this.exited) {
        throw new Error(`process exited while waiting for ${event}`);
      }
      const left = deadline - Date.now();
      if (left <= 0) {
        throw new Error(`timeout waiting for ${event}`);
      }
      await new Promise<void>(resolve => {
        const t = setTimeout(resolve, left);
        this.waiters.push(() => {
          clearTimeout(t);
          resolve();
        });
      });
    }
  }

  async stop(timeoutMs = 20_000) {
    if (this.exited || !this.proc.pid) {
      return;
    }
    const exited = new Promise<void>(resolve => this.proc.once('exit', () => resolve()));
    const t = setTimeout(() => {
      try {
        process.kill(-this.proc.pid!, 'SIGKILL');
      } catch {
        // already gone
      }
    }, timeoutMs);
    await exited;
    clearTimeout(t);
  }
}

const env = (key: string): string => {
  const v = process.env[key];
  if (!v) {
    throw new Error(`${key} not set`);
  }
  return v;
};

type TShot = { label: string; file: string };
type TStepRecord = {
  step: string;
  method?: string;
  chainId?: string;
  dialog?: boolean;
  dapp?: TEvent;
  app: TShot[];
};

// Banners (testnet, update available, simulator attestation failure, microSD) live in
// ContentWrappers. Whether the attestation one is up when a screenshot is taken is a race, and the
// update one follows the latest release, so screenshots leave all of them out. Scrollbars too: the
// sidebar's overlay scrollbar is sometimes still visible after navigation.
const SCREENSHOT_CSS = '[class*="_contentWrapper_"] { display: none !important; } * { scrollbar-width: none !important; }';

// Tall enough for the WalletConnect form below the app's banners (testnet, attestation, update).
test.use({ viewport: { width: 1280, height: 1100 } });

let device: LineProc | undefined;
let dapp: LineProc | undefined;
let servewallet: ServeWallet | undefined;

test('WalletConnect: dapp requests confirmed on the device', async ({ page, host, frontendPort, servewalletPort }, testInfo) => {
  test.setTimeout(20 * 60_000);
  const out = env('WC_OUT');
  const appDir = path.join(out, 'app');
  fs.mkdirSync(appDir, { recursive: true });
  const steps: TStepRecord[] = [];
  let shotIndex = 0;
  const writeSteps = () => fs.writeFileSync(path.join(appDir, 'steps.json'), JSON.stringify(steps, null, 2));

  // Animations off and caret hidden so a screenshot depends on the state, not on its timing.
  const shot = async (rec: TStepRecord, label: string, p: Page = page) => {
    const file = `${String(shotIndex++).padStart(2, '0')}-${rec.step}-${label}.png`;
    await p.screenshot({ path: path.join(appDir, file), animations: 'disabled', caret: 'hide', style: SCREENSHOT_CSS });
    rec.app.push({ label, file });
    writeSteps();
  };
  const begin = (step: string): TStepRecord => {
    const rec: TStepRecord = { step, app: [] };
    steps.push(rec);
    return rec;
  };
  const deviceStep = async (name: string) => {
    device!.send(`step ${name}`);
    await device!.next('step', 60_000);
  };

  await test.step('Start device and app', async () => {
    deleteAccountsFile();
    deleteConfigFile();
    device = new LineProc('device', env('WC_DEVICE'), [
      '--sim-dir', env('SIMULATOR_DIR'), '--out', path.join(out, 'device'),
      '--display', process.env.E2E_DISPLAY || ':72',
    ], path.join(out, 'device.log'));
    await device.next('ready', 120_000);
    servewallet = new ServeWallet(page, servewalletPort, frontendPort, host, testInfo.outputDir,
      { simulator: true, timeout: 300_000 });
    await servewallet.start();
  });

  let accountCode = '';
  await test.step('Unlock, pair and find the ETH account', async () => {
    // The device auto-confirms password entry and the pairing code; the app may ask to continue
    // or to skip a firmware upgrade on the way.
    const deadline = Date.now() + 180_000;
    while (Date.now() < deadline) {
      const resp = await page.request.get(`http://${host}:${servewalletPort}/api/accounts`);
      const accounts = resp.ok() ? await resp.json() as Array<{ code: string; coinCode: string }> : [];
      const eth = Array.isArray(accounts) ? accounts.find(a => a.coinCode === 'sepeth') : undefined;
      if (eth) {
        accountCode = eth.code;
        break;
      }
      for (const name of ['Continue', 'Skip']) {
        const b = page.getByRole('button', { name, exact: true });
        if (await b.isVisible()) {
          await b.click().catch(() => undefined);
        }
      }
      await page.waitForTimeout(500);
    }
    expect(accountCode, 'no Sepolia ETH account after pairing').not.toBe('');
  });

  let uri = '';
  await test.step('Open WalletConnect and pair', async () => {
    const rec = begin('pairing');
    await deviceStep('pairing');
    dapp = new LineProc('dapp', 'node', [env('WC_DAPP')], path.join(out, 'dapp.log'));
    uri = (await dapp.next('uri', 60_000)).uri as string;

    await page.goto(`/#/account/${accountCode}/wallet-connect/dashboard`);
    const newConnection = page.getByText('New connection', { exact: true });
    await expect(newConnection).toBeVisible({ timeout: 60_000 });
    await shot(rec, 'dashboard');
    await newConnection.click();

    // The label is not associated with the input.
    const input = page.locator('form input').first();
    await expect(input).toBeVisible({ timeout: 60_000 });
    await input.fill(uri);
    await page.getByRole('button', { name: 'Connect', exact: true }).click();

    const approve = page.getByRole('button', { name: 'Approve Connection' });
    await expect(approve).toBeVisible({ timeout: 60_000 });
    await shot(rec, 'proposal');
    await approve.click();
    const session = await dapp.next('session', 60_000);
    rec.dapp = session;
    await expect(page.getByText('Dapp successfully connected', { exact: false })).toBeVisible({ timeout: 30_000 });
    await shot(rec, 'paired');
  });

  const requests = (await new Promise<string>((resolve, reject) => {
    const p = spawn('node', [env('WC_DAPP'), '--list']);
    let s = '';
    p.stdout.on('data', d => (s += String(d)));
    p.on('exit', code => (code === 0 ? resolve(s) : reject(new Error(`dapp --list exited ${String(code)}`))));
  })).trim().split('\n').map(l => l.split('\t')[0]!);

  for (const step of requests) {
    await test.step(`Request ${step}`, async () => {
      const rec = begin(step);
      await deviceStep(step);
      dapp!.send('next');
      const req = await dapp!.next('request', 30_000);
      rec.method = req.method as string;
      rec.chainId = req.chainId as string;
      const result = dapp!.next('result', 300_000);
      // A request the app or dapp rejects outright never opens the dialog.
      const dialog = page.getByRole('heading', { name: 'WalletConnect request' });
      const first = await Promise.race([
        dialog.waitFor({ state: 'visible', timeout: 120_000 }).then(() => 'dialog'),
        result.then(() => 'result'),
      ]);
      rec.dialog = first === 'dialog';
      if (rec.dialog) {
        await shot(rec, 'request');
        await page.getByRole('button', { name: 'Continue', exact: true }).click();
      }
      rec.dapp = await result;
      writeSteps();
      if (rec.dialog) {
        const success = page.getByText('Request succesfully signed');
        const ok = page.getByRole('button', { name: 'OK', exact: true });
        await expect(success.or(ok)).toBeVisible({ timeout: 30_000 });
        await shot(rec, 'result');
        if (await ok.isVisible()) {
          await ok.click();
        }
        // The success state closes itself after 5s; the next request would replace it anyway,
        // but waiting keeps its screenshot free of the previous dialog.
        await expect(dialog).toBeHidden({ timeout: 15_000 });
      } else {
        await shot(rec, 'result');
      }
    });
  }

  await test.step('Close', async () => {
    dapp!.send('quit');
    await dapp!.next('done', 30_000).catch(() => undefined);
    await deviceStep('end');
  });
});

test.afterAll(async () => {
  await dapp?.stop(10_000);
  await servewallet?.stop();
  if (device) {
    device.send('quit');
    await device.stop(30_000);
  }
});
