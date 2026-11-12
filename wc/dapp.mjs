#!/usr/bin/env node
// WalletConnect test dapp for the BitBoxApp e2e flow.
//
// Protocol: one JSON object per line. stdout carries events, stdin carries commands; everything
// else (WalletConnect logs) goes to stderr.
//
//   -> {"event":"uri","uri":"wc:..."}             pairing URI to paste into the app
//   -> {"event":"session","address":"0x..","chains":["eip155:1",...],"methods":[...]}
//   <- next                                        send the next request of REQUESTS
//   -> {"event":"request","step":..,"method":..,"chainId":..,"params":..}
//   -> {"event":"result","step":..,"ok":true,"result":..,"check":{..}} | {"ok":false,"error":{..}}
//   -> {"event":"done"}                            after the last request, or on "quit"
//
// `check` is the dapp-side verification: recovered signer vs session address.
//
//   node dapp.mjs [--list]        --list prints the request steps and exits

import { SignClient } from '@walletconnect/sign-client';
import { createInterface } from 'node:readline';
import {
  hashTypedData,
  parseTransaction,
  recoverAddress,
  recoverMessageAddress,
  recoverTransactionAddress,
  toHex,
} from 'viem';

// Same project ID as the app (WCWeb3WalletProvider.tsx). Override for a dedicated e2e project.
const PROJECT_ID = process.env.WC_PROJECT_ID || '89733df088867a1a1bf644013addd6cc';
const TIMEOUT_MS = 180_000;

// Requested as optional namespaces: the app approves the subset it supports, so refs with fewer
// chains still pair, and requests on chains outside the session fail at the dapp.
const CHAINS = [1, 10, 56, 137, 8453, 42161, 11155111].map(id => `eip155:${id}`);
const METHODS = ['personal_sign', 'eth_signTypedData_v4', 'eth_signTransaction', 'eth_sendTransaction'];

// Fixed recipient so device screens are comparable between runs.
const RECIPIENT = '0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045';

const typedData = chainId => ({
  types: {
    EIP712Domain: [
      { name: 'name', type: 'string' },
      { name: 'version', type: 'string' },
      { name: 'chainId', type: 'uint256' },
      { name: 'verifyingContract', type: 'address' },
    ],
    Person: [
      { name: 'name', type: 'string' },
      { name: 'wallet', type: 'address' },
    ],
    Mail: [
      { name: 'from', type: 'Person' },
      { name: 'to', type: 'Person' },
      { name: 'contents', type: 'string' },
    ],
  },
  primaryType: 'Mail',
  domain: {
    name: 'BitBox e2e',
    version: '1',
    chainId,
    verifyingContract: '0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC',
  },
  message: {
    from: { name: 'Alice', wallet: '0xCD2a3d9F938E13CD947Ec05AbC7FE734Df8DD826' },
    to: { name: 'Bob', wallet: '0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB' },
    contents: 'Hello from the BitBox e2e dapp',
  },
});

const tx = (address, extra = {}) => ({ from: address, to: RECIPIENT, value: '0x0', data: '0x', ...extra });

// The value is 0: the backend estimates gas with the sender's (empty) balance, so a non-zero
// value fails before the device is asked. A fixed nonce keeps it off the network for signTransaction.
export const REQUESTS = [
  {
    step: 'personal-sign-sepolia',
    chainId: 'eip155:11155111',
    method: 'personal_sign',
    params: a => [toHex('BitBox e2e: personal_sign over WalletConnect'), a],
  },
  {
    step: 'typed-data-sepolia',
    chainId: 'eip155:11155111',
    method: 'eth_signTypedData_v4',
    params: a => [a, JSON.stringify(typedData(11155111))],
  },
  {
    // Sepolia account keypath (m/44'/1') on mainnet: the device warns about the unusual keypath.
    step: 'typed-data-mainnet',
    chainId: 'eip155:1',
    method: 'eth_signTypedData_v4',
    params: a => [a, JSON.stringify(typedData(1))],
  },
  {
    step: 'sign-tx-sepolia',
    chainId: 'eip155:11155111',
    method: 'eth_signTransaction',
    params: a => [tx(a, { nonce: '0x0' })],
  },
  {
    // Cross-chain sends are signed on the device, then broadcast fails (unfunded simulator seed).
    step: 'send-tx-arbitrum',
    chainId: 'eip155:42161',
    method: 'eth_sendTransaction',
    params: a => [tx(a)],
  },
  {
    step: 'send-tx-polygon',
    chainId: 'eip155:137',
    method: 'eth_sendTransaction',
    params: a => [tx(a)],
  },
];

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

async function check(req, address, params, result) {
  const lower = s => s.toLowerCase();
  let signer;
  if (req.method === 'personal_sign') {
    signer = await recoverMessageAddress({ message: { raw: params[0] }, signature: result });
  } else if (req.method === 'eth_signTypedData_v4') {
    signer = await recoverAddress({ hash: hashTypedData(JSON.parse(params[1])), signature: result });
  } else if (req.method === 'eth_signTransaction') {
    const parsed = parseTransaction(result);
    signer = await recoverTransactionAddress({ serializedTransaction: result });
    return {
      signerMatches: lower(signer) === lower(address),
      signer,
      chainId: parsed.chainId,
      to: parsed.to,
      nonce: parsed.nonce,
    };
  } else {
    return { txHash: result };
  }
  return { signerMatches: lower(signer) === lower(address), signer };
}

async function main() {
  if (process.argv.includes('--list')) {
    for (const r of REQUESTS) {
      console.log(`${r.step}\t${r.method}\t${r.chainId}`);
    }
    return;
  }

  const client = await SignClient.init({
    projectId: PROJECT_ID,
    logger: process.env.WC_LOG || 'error',
    metadata: {
      name: 'BitBox e2e dapp',
      description: 'WalletConnect test dapp',
      url: 'https://e2e.bitbox.invalid',
      // No icon: the app would fetch it, which makes screenshots depend on the network.
      icons: [],
    },
  });

  const { uri, approval } = await client.connect({
    optionalNamespaces: {
      eip155: { chains: CHAINS, methods: METHODS, events: ['chainChanged', 'accountsChanged'] },
    },
  });
  emit({ event: 'uri', uri });

  const session = await approval();
  const ns = session.namespaces.eip155;
  const address = ns.accounts[0].split(':')[2];
  emit({ event: 'session', address, chains: ns.chains ?? [], methods: ns.methods, accounts: ns.accounts });

  const lines = createInterface({ input: process.stdin });
  let i = 0;
  for await (const line of lines) {
    const cmd = line.trim();
    if (cmd === 'quit' || i >= REQUESTS.length) {
      break;
    }
    if (cmd !== 'next') {
      continue;
    }
    const req = REQUESTS[i++];
    const params = req.params(address);
    emit({ event: 'request', step: req.step, method: req.method, chainId: req.chainId, params });
    try {
      const result = await Promise.race([
        client.request({ topic: session.topic, chainId: req.chainId, request: { method: req.method, params } }),
        new Promise((_, reject) => setTimeout(() => reject(new Error('dapp timeout')), TIMEOUT_MS)),
      ]);
      emit({ event: 'result', step: req.step, ok: true, result, check: await check(req, address, params, result) });
    } catch (err) {
      const error = err instanceof Error ? { message: err.message } : err;
      emit({ event: 'result', step: req.step, ok: false, error });
    }
    if (i >= REQUESTS.length) {
      break;
    }
  }
  emit({ event: 'done' });
  try {
    await client.disconnect({ topic: session.topic, reason: { code: 6000, message: 'e2e done' } });
  } catch {
    // Best effort: the app may already be gone.
  }
  process.exit(0);
}

main().catch(err => {
  console.error(err);
  emit({ event: 'error', message: String(err?.message ?? err) });
  process.exit(1);
});
