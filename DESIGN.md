# Design: automatic e2e evidence and regression per PR

Goal: every firmware/API/app change gets an automatic e2e run that (A) shows the maintainer what
the change looks like on the device, as screens that differ from the merge-base, and (B) flags
anything pre-existing that broke. Expected results are optional; they accumulate when a
maintainer promotes approved screens.

## Lanes

- **A, evidence** (`e2e.py pr`): run the units selected for the change on the head simulator and
  diff each against the merge-base baseline bundle. Report only differing/new screens, plus the
  changed firmware functions that no unit reached.
- **B, regression** (`e2e.py pr`, same run): every unit that has expectations is checked on head
  (pass/fail), and the api-go/api-rs/api-ts simulator suites run against the head simulator,
  once with their committed vector copies (drift) and once with the firmware's vectors.

## Units

A unit is one scenario run. `units.load_units(firmware_dir, scenarios_dir)` returns a list of

```python
{
  "id": "scenario/eth-erc20-known" | "btc-vector/<vector name>",
  "kind": "scenario" | "btc-vector",
  "scenario": {"name": ..., "description": ..., "steps": [...]},  # driver input, same as scenarios/*.json
  "expect": [ {"min_version": "9.28.0", "max_version_exclusive": null, "screens": [...]}, ... ] | None,
}
```

- `scenarios/*.json` units: `expect` comes from an optional `expect` key in the file.
- BTC vector units come from `<firmware>/src/rust/bitbox-test-vectors/testdata/btc-transaction-test-vectors.json`; one unit per
  vector, `expect` from its version-ranged expectations. The driver op is `btc-sign-psbt`.
- `expect.check(unit, screens, simulator_version) -> {"status": "pass"|"fail"|"no-expectation", "diff": str}`
  picks the expectation whose version range contains the simulator version.
- `e2e.py promote <pr-out-dir> <unit-id>...` writes head screens into the scenario's `expect`
  for the head simulator version (scenario units only; vectors are owned by the firmware repo).

Screens are the `screens.json` list `run_headless` writes (headless simulator transcript).

## Baseline bundle

`e2e.py baseline <firmware-ref> [--graphical-units <glob>...]` writes
`$BITBOX_E2E_CACHE/baselines/<sha>/`:

```
manifest.json                 {sha, simulator_version, harness_sha, created, units: {id: {headless_ok, graphical_ok}}}
units/<id-slug>/headless/     screens.json, combined.log, driver.jsonl
units/<id-slug>/graphical/    frames.json, *.png      (only units selected for graphical)
apis/                         lane-B results for this ref
coverage/map.json             function -> unit ids (from the instrumented build)
```

PR runs reuse it (keyed by the merge-base sha) and only run head. CI publishes the directory as a
tarball (release asset); locally it lives in the cache.

## Coverage map and selection

- `build-sim.sh --coverage <ref>`: instrumented headless simulator at `sims/<sha>-cov/`.
- `coverage.py map <ref>` runs every unit headless on it, one profile per unit, and writes
  `coverage/map.json`: `{"functions": {"<file>::<function>": [unit ids]}, "lines": {"<file>": {"<line>": [unit ids]}}}`.
- `coverage.py select --base <sha> --head <ref>`: changed lines in Rust files linked into the
  simulator → units to run, and the changed functions/lines that no unit executes. Changed lines in
  head that do not exist in base are mapped by function name.

## Parallel runs

Both simulators bind 127.0.0.1:15423. Wrap runs in `isolated.sh` (private network namespace) and
give each concurrent run its own `E2E_DISPLAY`. Build outside the namespace (no network inside).
`E2E_DRIVER=<path>` uses a prebuilt driver and skips the cargo build.
