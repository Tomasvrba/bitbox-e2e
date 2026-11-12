// Smoke test for sim_bridge.py: connect through it with @bitboxswiss/bitbox-api the way Rabby
// does (bitbox02ConnectBridge), pair, query the device, disconnect, and do it again on a fresh
// socket (Rabby reconnects whenever its offscreen document is recreated).
//
//   node check_bridge.mjs <path to node_modules/@bitboxswiss/bitbox-api/dist/index.js> [rounds]
// Needs a simulator behind the bridge that confirms pairing on its own (the headless one does).
const [lib, rounds = '2'] = process.argv.slice(2);
const bitbox = await import(lib);

for (let i = 0; i < Number(rounds); i++) {
  const device = await bitbox.bitbox02ConnectBridge(() => {});
  const pairing = await device.unlockAndPair();
  const code = pairing.getPairingCode();
  const paired = await pairing.waitConfirm();
  const info = await paired.deviceInfo();
  console.log(JSON.stringify({ round: i, pairingCode: code !== undefined, version: paired.version(), info }));
  paired.close();
  // Give the bridge time to see the socket close.
  await new Promise((r) => setTimeout(r, 300));
}
process.exit(0);
