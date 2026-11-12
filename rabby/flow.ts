// Drives the unpacked Rabby extension through onboarding with a BitBox02 and a fixed set of dapp
// requests. Started by run.py, which owns the simulator and confirms on the device.
//
//   node flow.ts --ext <rabby dist> --out <dir> --dapp <url> [--proxy <host:port>]
//
// Protocol (same as the Rust driver): one JSON event per stdout line. After each
// {"event":"step-start"} we wait for "go" on stdin, so run.py labels device frames with the right
// step before anything reaches the device. Screenshots: {"event":"shot","step","file"}.
import { chromium } from 'playwright';
import type { BrowserContext, Page } from 'playwright';
import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const arg = (name: string, def?: string): string => {
  const i = process.argv.indexOf(`--${name}`);
  if (i >= 0) return process.argv[i + 1];
  if (def === undefined) throw new Error(`--${name} is required`);
  return def;
};
const EXT = path.resolve(arg('ext'));
const OUT = path.resolve(arg('out'));
const DAPP = arg('dapp');
const PROXY = arg('proxy', '');
const PASSWORD = 'bitbox-e2e-password';

// Anvil chains run.py starts, as Rabby's custom RPC for the built-in chain of the same id.
const RPC: Record<string, string> = {
  Ethereum: 'http://127.0.0.1:8545',
  Arbitrum: 'http://127.0.0.1:8546',
  'BNB Chain': 'http://127.0.0.1:8547',
};
const RECIPIENT = '0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045';
// Real token addresses; run.py puts fixtures/TestToken.sol there. The firmware knows mainnet USDC,
// not the Arbitrum one.
const USDC_ETH = '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48';
const USDC_ARB = '0xaf88d065e77c8cC2239327C5EDB3A432268e5831';

const emit = (ev: object) => process.stdout.write(JSON.stringify(ev) + '\n');
const stdin = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

let shotNo = 0;
async function shot(page: Page, step: string, what: string) {
  const file = `rabby/${String(shotNo++).padStart(2, '0')}-${step}-${what}.png`;
  // Rabby pulses a status dot next to custom-RPC chains; frozen animations keep shots comparable.
  await page.screenshot({ path: path.join(OUT, file), animations: 'disabled', caret: 'hide' });
  emit({ event: 'shot', step, file, what });
}

// Wait until the page text stopped changing: Rabby fills the approval in from several async
// sources (simulation, security checks, gas), and a screenshot taken half way is not reproducible.
async function settle(page: Page, quietMs = 1500, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let last = '';
  let since = Date.now();
  while (Date.now() < deadline) {
    const t = await page.locator('body').innerText().catch(() => '');
    if (t !== last) {
      last = t;
      since = Date.now();
    } else if (Date.now() - since >= quietMs) {
      return;
    }
    await sleep(100);
  }
}

// `before`: pages that existed before the action; Rabby's previous popup may still be closing.
async function waitPage(ctx: BrowserContext, match: (url: string) => boolean, before = new Set<Page>(), timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const p = ctx.pages().find((x) => !before.has(x) && !x.isClosed() && match(x.url()));
    if (p) return p;
    await sleep(100);
  }
  throw new Error('page did not open');
}

const isPopup = (url: string) => url.includes('/notification.html');

async function onboard(ctx: BrowserContext) {
  const p = await waitPage(ctx, (u) => u.includes('#/new-user/'));
  await p.getByText('I already have an address').click();
  await p.getByText('Hardware Wallet').click();
  await p.getByText('BitBox02', { exact: true }).click();
  await p.getByPlaceholder('Password (8 characters min)').fill(PASSWORD);
  await p.getByPlaceholder('Confirm Password').fill(PASSWORD);
  await p.getByRole('button', { name: 'Confirm' }).click();
  await shot(p, 'pairing', 'connect');
  // Unlock + pairing on the device; run.py confirms both. Under load the firmware sometimes drops
  // the unlock request ("error querying device info"); clicking again picks up where it stopped.
  for (let i = 0; i < 4; i++) {
    await p.getByRole('button', { name: 'Connect BitBox02' }).click();
    try {
      await p.waitForURL(/#\/new-user\/success/, { timeout: 60000 });
      break;
    } catch (e) {
      if (i === 3) throw e;
    }
  }
  await settle(p);
  await shot(p, 'pairing', 'imported');
  return p;
}

async function setCustomRpcs(ctx: BrowserContext, extId: string) {
  const p = await ctx.newPage();
  await p.goto(`chrome-extension://${extId}/popup.html#/custom-rpc`);
  for (const [chain, url] of Object.entries(RPC)) {
    await p.getByRole('button', { name: 'Modify RPC URL' }).click();
    await p.getByText(chain, { exact: true }).last().click();
    await p.locator('input:visible').last().fill(url);
    await p.getByRole('button', { name: 'Save' }).click();
    await p.getByText(url).waitFor();
  }
  await shot(p, 'pairing', 'custom-rpc');
  await p.close();
}

// A dapp request whose approval opens Rabby's notification popup. Returns once the dapp got its
// answer, i.e. after the device confirmed.
async function approve(ctx: BrowserContext, dapp: Page, step: string, method: string, params: unknown[]) {
  const before = new Set(ctx.pages());
  const answer = dapp.evaluate(([m, p]) => (window as any).call(m, p), [method, params] as const);
  const popup = await waitPage(ctx, isPopup, before);
  await popup.getByRole('button', { name: /Begin signing/ }).waitFor();
  await settle(popup);
  await shot(popup, step, 'approval');
  const ignore = popup.getByText('Ignore all');
  if (await ignore.count()) await ignore.first().click();
  await popup.getByRole('button', { name: /Begin signing/ }).click();
  // Rabby offers "Retry" when the device request failed (busy machine); the firmware screen
  // sequence then starts over, which run.py records like any other. Reported as a retry event.
  let res: { ok?: unknown; err?: string } | undefined;
  answer.then((r) => { res = r as typeof res; });
  const retry = popup.getByRole('button', { name: 'Retry' });
  for (let retries = 0, deadline = Date.now() + 240000; !res; await sleep(300)) {
    if (Date.now() > deadline) throw new Error(`${method}: no answer`);
    if (retries < 2 && !popup.isClosed() && (await retry.isVisible().catch(() => false))) {
      retries++;
      emit({ event: 'retry', step, text: await popup.locator('body').innerText().catch(() => '') });
      await retry.click().catch(() => {});
    }
  }
  if (res.err) throw new Error(`${method}: ${res.err}`);
  await settle(dapp, 500);
  await shot(dapp, step, 'dapp');
  return res.ok;
}

async function main() {
  fs.mkdirSync(path.join(OUT, 'rabby'), { recursive: true });
  const profile = path.join(OUT, 'profile');
  fs.rmSync(profile, { recursive: true, force: true });
  const args = [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`];
  if (PROXY) args.push(`--proxy-server=http://${PROXY}`, '--ignore-certificate-errors');
  const ctx = await chromium.launchPersistentContext(profile, {
    headless: false,
    args,
    // Rabby's popup layout; the onboarding pages adapt.
    viewport: { width: 400, height: 720 },
  });
  const consoleLog = fs.createWriteStream(path.join(OUT, 'browser-console.log'));
  const hook = (w: { on: Function; url: () => string }, tag: string) =>
    w.on('console', (m: any) => consoleLog.write(`[${tag}] ${m.type()} ${m.text()}\n`));
  ctx.serviceWorkers().forEach((w) => hook(w, 'sw'));
  ctx.on('serviceworker', (w) => hook(w, 'sw'));
  ctx.on('page', (p) => hook(p, 'page'));

  let current = 'pairing';
  const step = async (name: string, fn: () => Promise<unknown>) => {
    current = name;
    emit({ event: 'step-start', step: name });
    await stdin.next();
    const result = await fn();
    emit({ event: 'step-end', step: name, result: result ?? {} });
  };

  try {
    let addr = '';
    let dapp!: Page;
    await step('pairing', async () => {
      const onb = await onboard(ctx);
      const extId = new URL(onb.url()).host;
      await setCustomRpcs(ctx, extId);
      dapp = await ctx.newPage();
      await dapp.goto(DAPP);
      const before = new Set(ctx.pages());
      const accounts = dapp.evaluate(() => (window as any).call('eth_requestAccounts', []));
      const popup = await waitPage(ctx, isPopup, before);
      await popup.getByRole('button', { name: 'Connect' }).waitFor();
      await settle(popup);
      await shot(popup, 'pairing', 'dapp-connect');
      const ignore = popup.getByText('Ignore all');
      if (await ignore.count()) await ignore.first().click();
      await popup.getByRole('button', { name: 'Connect' }).click();
      const res = (await accounts) as { ok?: string[]; err?: string };
      if (!res.ok) throw new Error(`eth_requestAccounts: ${res.err}`);
      addr = res.ok[0];
      return { address: addr };
    });
    const from = addr;
    const transfer = (token: string, amount: bigint) => ({
      from,
      to: token,
      value: '0x0',
      data:
        '0xa9059cbb' +
        RECIPIENT.slice(2).toLowerCase().padStart(64, '0') +
        amount.toString(16).padStart(64, '0'),
    });
    const switchTo = (chainId: string) => async () => {
      const r = (await dapp.evaluate((c) => (window as any).call('wallet_switchEthereumChain', [{ chainId: c }]), chainId)) as { err?: string };
      if (r.err) throw new Error(`switch to ${chainId}: ${r.err}`);
      await settle(dapp, 500);
      await shot(dapp, current, 'dapp');
      return { chainId };
    };

    await step('eth-personal-sign', () =>
      approve(ctx, dapp, current, 'personal_sign', [
        '0x' + Buffer.from('Hello from the BitBox e2e dapp').toString('hex'),
        from,
      ]));
    await step('eth-typed-data-v4', () =>
      approve(ctx, dapp, current, 'eth_signTypedData_v4', [from, JSON.stringify({
        types: {
          EIP712Domain: [
            { name: 'name', type: 'string' },
            { name: 'version', type: 'string' },
            { name: 'chainId', type: 'uint256' },
            { name: 'verifyingContract', type: 'address' },
          ],
          Person: [{ name: 'name', type: 'string' }, { name: 'wallet', type: 'address' }],
          Mail: [
            { name: 'from', type: 'Person' },
            { name: 'to', type: 'Person' },
            { name: 'contents', type: 'string' },
          ],
        },
        primaryType: 'Mail',
        domain: { name: 'Ether Mail', version: '1', chainId: 1, verifyingContract: '0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC' },
        message: {
          from: { name: 'Cow', wallet: '0xCD2a3d9F938E13CD947Ec05AbC7FE734Df8DD826' },
          to: { name: 'Bob', wallet: '0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB' },
          contents: 'Hello, Bob!',
        },
      })]));
    await step('eth-send-1559', () =>
      approve(ctx, dapp, current, 'eth_sendTransaction', [{ from, to: RECIPIENT, value: '0x38d7ea4c68000' }]));
    await step('eth-erc20-transfer', () =>
      approve(ctx, dapp, current, 'eth_sendTransaction', [transfer(USDC_ETH, 2_500_000n)]));
    await step('switch-arbitrum', switchTo('0xa4b1'));
    await step('arb-send-1559', () =>
      approve(ctx, dapp, current, 'eth_sendTransaction', [{ from, to: RECIPIENT, value: '0x2386f26fc10000' }]));
    await step('arb-erc20-transfer', () =>
      approve(ctx, dapp, current, 'eth_sendTransaction', [transfer(USDC_ARB, 1_500_000n)]));
    // Rabby has EIP-1559 off for BNB Chain, so this is the legacy (type 0) signing path.
    await step('switch-bsc', switchTo('0x38'));
    await step('bsc-send-legacy', () =>
      approve(ctx, dapp, current, 'eth_sendTransaction', [{ from, to: RECIPIENT, value: '0x470de4df820000', gasPrice: '0x3b9aca00' }]));
    emit({ event: 'done' });
  } catch (e) {
    for (const p of ctx.pages()) {
      await p.screenshot({ path: path.join(OUT, `error-${ctx.pages().indexOf(p)}.png`) }).catch(() => {});
    }
    emit({ event: 'step-end', step: current, error: String((e as Error).stack ?? e) });
    emit({ event: 'done', error: String(e) });
    process.exitCode = 1;
  } finally {
    await ctx.close();
    consoleLog.end();
    process.exit();
  }
}

main();
