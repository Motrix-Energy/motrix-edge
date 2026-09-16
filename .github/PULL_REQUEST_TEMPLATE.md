<!--
Thanks for contributing. The recipes and contracts are in CONTRIBUTING.md; this is the short version
of its bar. Explain *why* in the description — the what is in the diff.
-->

## What this changes, and why

<!-- If it fixes an issue, link it. If it exists because something broke, say what broke. -->

## Checklist

- [ ] `pytest` is green — the full suite, not `-m "not slow"`
- [ ] `ruff check .` is clean, and **no formatter has touched the tree** (the codebase is tabs and
      deliberately unformatted; a reformat buries the real change)
- [ ] `python examples/generate.py --check` exits 0 — required if the change is anywhere near
      `storage/`. An unexpected diff is a stop sign, not a fixture to regenerate
- [ ] Tabs, in Python and JSON alike; logging through `self.LOGGER`, never `print()`
- [ ] Nothing from a real installation in any config, fixture, test, comment or log sample

If this adds a plugin:

- [ ] It lives entirely inside its axis package — no central file edited
- [ ] Its schema file ships, and its keys are in lockstep with the constructor signature
- [ ] Its test file is copied from the matching template, and mocks or scripts its transport — no
      broker, no serial port, no network
- [ ] An optional dependency gets its own `requirements-<name>.txt`, a `-r` line in
      `requirements-dev.txt`, and `pytest.importorskip` before any project import in its tests
- [ ] The [plugin catalogue](https://motrix-energy.github.io/reference/plugin-catalog/) row is added
      or updated

If this touches the CSV output in any way, stop and read
[Changing the storage format](https://motrix-energy.github.io/contribute/storage-format-changes/)
first — it spans both repositories.
