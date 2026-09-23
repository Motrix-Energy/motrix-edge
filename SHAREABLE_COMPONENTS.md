# Sharing connectors, algorithms and protocols

**Status: implemented, 2026-09-21.** All four commits landed, in the order 1 → 1b → 3 → 2 → 4 rather
than 1 → 2 → 3 → 4: commit 3's MRO walk turned out to be a hard prerequisite for commit 2's algorithm
schemas, and a fifth small commit (1b) carries the §5 storage warning, which §7 described but never
scheduled. The prose below is left as written, as the plan it was, with the statements the work
falsified corrected in place and marked. Every factual claim about the code was checked against the
tree on 2026-09-18 and re-checked on implementation — see [Appendix A](#appendix-a--what-was-actually-run).

Four things in this document turned out to be wrong or incomplete, and each is corrected where it
appears: `Device.control` is at `api/device.py:231-238`, not `:103-106`; `.github/ISSUE_TEMPLATE/config.yml`
already existed, so commit 4 edited it rather than adding it; the `role` enum is in four schemas, not
two; and §11 listed four cross-repo consequences where there were ten. Three things the plan asked
for could not be done as written, and are noted at §5 and §7 commit 2.

---

## The question

The three axes are dissociated: an algorithm does not know what transport fed it, a connector does not
know what logic consumes it, a device does not know either. That is done. What is not done is the
consequence: if a stranger writes a connector for their inverter, or an algorithm for their tariff,
**how do they publish it, and how does someone else get it into a running installation?**

## The short answer

They already can, and that is the surprise. A third-party plugin at `connectors/<vendor>/<name>.py`
loads today, unmodified, under the shipped `connectors/__init__.py` — it was run to confirm it. So the
roadmap is not about *enabling* sharing. It is about four other things, in this order:

> **survive → validate → extract → document**

Survive a stranger's bug. Validate a stranger's options. Extract the test harness so a stranger can
check their own work. Then — and only then — write down the convention. Discovery, an index, PyPI,
version handshakes and everything else become **options with written entry conditions**, not scheduled
phases. A young project's expensive mistake is building an ecosystem's infrastructure before the
ecosystem exists.

---

## 1. Your three words are four artifacts

You said "connectors, algorithms and protocols". Two of those are Python. The third is usually not,
and that is the most useful thing in this document.

| You said | What it is here | Retrieval path | Python? |
|---|---|---|---|
| **a connector** | a module on the `connectors/` axis | copy a directory | yes |
| **an algorithm** | a module on the `algorithms/` axis | copy a directory | yes |
| **a protocol** — new transport | a connector (MQTT, Modbus TCP, serial LoRa) | copy a directory | yes |
| **a protocol** — new *dialect* of a transport | **config** | copy a JSON fragment | **no** |
| **a protocol** — new payload layout | **config** (register map, field map) | copy a JSON fragment | **no** |

The worked proof is already in the repo. Supporting **AWS IoT Core as a LoRaWAN network server** — a
server `connectors/lorawan.py` has never heard of — costs zero Python, because `profile: "custom"`
takes the topic templates and the downlink envelope from `config.json`:

```json
{
	"name": "aws",
	"protocol": "lorawan",
	"options": {
		"profile": "custom",
		"custom_uplink_topic": "lorawan/uplink/{dev_eui}",
		"custom_downlink_topic": "lorawan/downlink/{dev_eui}",
		"custom_downlink_body": { "PayloadData": "{payload_b64}", "WirelessMetadata": { "LoRaWAN": { "FPort": "{f_port}" } } }
	}
}
```

*(`examples/connectors/lorawan.json`, shipped and tested.)*

Same for hardware: a new Modbus meter is a `registers` array; a new LoRa node is a `fields` map.
Neither is code, neither needs trust, and both are reviewable by reading them.

**This splits the sharing problem in two, and the halves have completely different costs.** A shared
JSON fragment cannot execute, cannot reach a credential, and cannot crash the EMS. A shared Python
module can do all three. The plan below treats data as the default sharing unit and code as the case
that needs machinery.

---

## 2. The governing fact: external plugins already load

`main.create_classes` does `import_module(f"{package}.{clazz}")` and derives the expected class name
with `clazz.rsplit(".", 1)[-1]` — it has tolerated dotted module paths all along. `config.schema.json`
puts no pattern on `protocol`, `kind` or `class`. So:

```
connectors/acme/solar.py   containing   class SolarConnector(Connector)
config.json:  { "name": "roof", "protocol": "acme.solar", "options": { … } }
```

**works today.** Verified by execution: the module imports, `connectors.__path__` is unchanged, the
loader derives `solarconnector` and matches `SolarConnector`.

Two things follow, and both matter more than they look.

**The axis package stays regular.** A vendor sub-directory nests *inside* the existing package rather
than adding a second `sys.path` portion. `connectors/` keeps its `__init__.py`, so its `__path__` stays
a single directory. That is worth defending: if the five axis `__init__.py` files were deleted to make
the axes PEP 420 namespace packages, then *any* installed distribution shipping a top-level
`connectors/` directory joins the axis — and one shipping `connectors/__init__.py` **erases every
built-in connector**, from anywhere on `sys.path`, silently. That was reproduced. The nested layout
never has the hazard, so it needs no startup assertion, no CI guard and no regression test to defend.

**One thing is genuinely broken.** A dotted plugin name gets **no options validation at all**, silently
— not a warning, not a debug line. `config/config.py:28`'s `_PLUGIN_NAME` regex rejects the dot and
`continue`s before even the "no schema, skipping" debug line. And widening the regex alone is *not*
enough: `config/config.py:300` would then look for a literal `acme.solar.schema.json` instead of
`acme/solar.schema.json`. **Both lines or neither** — the regex alone converts a silent skip into a
silent miss, which is worse. Verified fix, both halves applied together:

```python
_PLUGIN_NAME = compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
schema_file = files(package).joinpath(f"{name.replace('.', '/')}.schema.json")
```

Keep `.fullmatch`. The regex is the **only** traversal guard there is: `files(pkg).joinpath()` performs
no containment check, so `../etc/passwd` escapes the portion and an absolute name escapes entirely.
(The *import* path needs no such guard — `import_module` raises `ModuleNotFoundError` for every hostile
name, which the loader already treats as a clean per-entry skip.)

---

## 3. Why the shipped connectors stay in-tree

A fair question once §2 lands: if a plugin can live in its own repository, why does cloning
`motrix-edge` still hand you eight connectors and eleven devices you will never configure? Should the
shipped ones not move out too, so an operator pulls only what they use?

**No, and the measurements say why.** All eight connectors are 162 KB of source, all eleven devices
141 KB, the whole repository 1.8 MB. More to the point, a connector that no `config.json` entry names
is **never imported** — `create_classes` imports exactly the modules the config names — and its library
never enters the environment, because every optional dependency lives in its own
`requirements-<extra>.txt`. An unused plugin costs disk and nothing else. There is no runtime weight to
remove.

**The cost that is real is not the cloner's, it is the maintainers'.** `tests/` is 12,719 lines, and
every in-tree plugin is one this project keeps green forever, against hardware it does not own. That is
the limit that actually binds, and it arrives long before disk space does — which is why the answer is
an admission policy (commit 4) rather than a repository split.

**`connectors/` holds two different kinds of thing, and only one of them would multiply.**

- *Reference implementations* — `mqtt`, `http_api`, `pseudo`, and the subclass pairs — are load-bearing
  infrastructure rather than a vendor catalogue. `lorawan` subclasses `mqtt`, `openems` subclasses
  `http_api`, `pseudo` drives the simulation clock and the entire deterministic-replay story, and all
  of them are the worked examples each recipe in `CONTRIBUTING.md` points at. The seam-extraction
  pattern that file documents *requires* the parent to be in-tree to extract a seam from. Moving these
  out would not slim the project; it would remove the thing new plugins are written against.
- *Vendor-specific plugins* are the category that grows without bound — two or three today. These are
  exactly the ones §2 already lets live in their author's repository, and the ones the admission policy
  keeps out.

**And splitting the existing tree is strictly more work than this plan, not less.**

1. A connector in its own repository still writes `from api.connector import Connector`. Under a vendor
   directory in-tree that resolves; as a separately-installed distribution it does not — there is no
   `pyproject.toml`, and fixing that means claiming `api`, `config`, `connectors`, `devices`, `storage`
   and `services` as flat top-level PyPI names, or rewriting ~218 imports across 76 files. "Install
   only the connector I want" is therefore a **superset** of triggered option 5, not an alternative to
   it.
2. It gives up atomic refactor. An ABC change currently moves all eight connectors in one commit with
   CI proving it — which is exactly how `Connector.deliver()` landed. Split, that becomes eight pull
   requests across eight repositories behind a version handshake, adopted before a single external
   contributor exists to justify it.

Home Assistant is the direct precedent and it points the same way: 1000+ integrations in one core
repository, deliberately, with HACS alongside rather than instead of it. What makes that bearable is
what this project already has — per-plugin optional dependencies and a loader that imports only what is
configured.

The one case where the instinct wins outright is a minimal image for a constrained gateway. At 162 KB,
it does not.

---

## 4. The one-way door: freeze the config value

Everything else in this plan is reversible in an afternoon. **The string an operator types into
`config.json` is not** — that file is mounted read-only precisely so upgrades never ask them to rewrite
it. Choose it once:

> **`"<vendor>.<name>"` → `<axis>/<vendor>/<name>.py` + `<axis>/<vendor>/<name>.schema.json`**

Not `community.<vendor>.<name>`. The bare vendor form survives unchanged if plugins ever move to a
separate `sys.path` root, to `motrix_edge/<axis>/<vendor>/`, or into wheels; a hard-coded `community.`
segment does not, and would have to be migrated in every deployment's config.

---

## 5. The five axes do not share alike

Nothing in the architecture says so, and an index or a recipe that treats them alike will be wrong.
Counts are from the tree; containment is from the code.

| Axis | Ships | Options schema | What a bad third-party one does | Recommend? |
|---|---|---|---|---|
| `storage/` | 3 | yes | contained per backend by `StorageManager`'s error isolation — but see the reserved-filename note below | safest |
| `devices/` | 11 | yes | a raise in `receive()` is now contained by `Connector.deliver()` | yes |
| `connectors/` | 8 | yes | owns a supervised thread; a raise is restarted with backoff | yes |
| `algorithms/` | 2 | yes *(was **no**; added by commit 2)* | holds `control_device` — actuates hardware *and* writes the versioned storage contract. Least contained. | yes, with eyes open |
| `services/` | 1 | yes | its constructor is typed against `Supervisor`, a concrete class from another package — **not writable against a published surface today** | not yet |

Two consequences the plan must carry:

- ~~**`algorithms/` is the one axis with no options validation**~~ — **done in commit 2.** It was the
  axis a community is most likely to contribute to, and the one whose `options` (`required_devices`,
  `delay_seconds`, `wait_for_devices_timeout`) most needed a declared contract.

  Adding the fifth call was one line, but the commit was not. Three things had to move with it, none
  of them in this plan:
  1. **The lockstep test could not see an algorithm's options at all.** It read
     `inspect.signature(cls.__init__)`, and both shipped algorithms are pure `**kwargs` forwarders — so
     any declared property failed as "options the constructor rejects". `api/conformance.py` now walks
     the MRO to the first `__init__` that does not forward. That is why commit 3 landed before this one.
  2. **`config.schema.json` already declared those three keys inline**, under the algorithms entry and
     nowhere else, with `additionalProperties: true` — and more strictly than interpolation allows
     (`delay_seconds` was `integer` only, so a `${TICK}` template failed validation). Relaxed to a bare
     `{"type": "object"}` like every other axis. Strictly widening, so not a `CONFIG_FORMAT_VERSION`
     change.
  3. **`Algorithm.__init__` assigned all three raw**, with no `api/options.py` coercion, so a schema
     that unions `"string"` (which it must, since validation precedes interpolation) would have
     legitimised values that `TypeError` on a supervised worker thread. Now coerced — with
     `wait_for_devices_timeout` handled bespoke, because `None` there means "wait forever" and a
     coercer that maps `None` to a default would silently delete that meaning.
- **The storage-contract exposure is a third-party *backend*, not a third-party device.** A device
  implementing `MetricSource` only names its own readings; it cannot move the CSV bytes. A *backend*
  can write `device_data.csv` into `data/storage` and produce a file the viewer identifies as
  format 1.0 by name and shape alone, with none of the byte-exactness `tests/test_storage_contract.py`
  guarantees. The mitigation is cheap: warn at startup when two registered backends resolve to the
  same `output_dir`, and write the rule that only a backend passing the fixture-conformance test may
  emit files under the reserved names.

---

## 6. The ceiling nobody will see until they hit it

This is the finding that matters most for **algorithms**, and it is not a packaging problem.

A shared algorithm is portable because it selects devices by capability ABC, never by class. The
entire capability vocabulary is:

- `EnergyMeter.get_total_energy_kwh()` — **cumulative imported kWh, and nothing else**
- `Switch` — a marker interface, **binary on/off**, actuated with a `str` token
- `MetricSource.get_metrics()` — a `{name: scalar}` view, for **storage**, invisible to algorithms

And the one hook a generic config-driven device has for saying what a number *means* — `role` in
`devices/modbus_meter.schema.json`, `devices/modbus_switch.schema.json`, `devices/lora.schema.json` and
`devices/lora_switch.schema.json` (four, not the two this document first claimed) — is a **closed enum with exactly one
member**, `energy_import_kwh`.

So a published algorithm can ask a device it has never seen exactly two questions: *how many kWh have
you imported in total*, and *are you switchable*. There is no instantaneous power, no battery state of
charge, no setpoint or modulation, no tariff or price signal, no forecast, no curtailment limit. Every
richer reading a device already parses flows to storage and is **unreachable from an algorithm**.

Nothing about publishing changes that. Two things follow:

1. **Say it out loud in the publishing recipe.** Telling authors "publish an algorithm" without
   telling them the ceiling sets them up to write code no shared device can feed.
2. **Widening the capability vocabulary is probably worth more to algorithm sharing than any packaging
   work in this document.** It is not scheduled here because it is a design question about an energy
   domain model, not a distribution question — but it belongs on the same page as the answer, because
   it is the real bottleneck. See [Appendix B](#appendix-b--open-questions).

---

## 7. Roadmap: four commits, then triggered options

Nothing in the first three commits mentions a plugin ecosystem. They are defect fixes and a refactor,
correct on their own terms even if no stranger ever writes a plugin. That is deliberate: it means the
first three quarters of this work carries no risk if the bet is wrong.

### Commit 1 — "One bad plugin is one skipped entry"

Every item is a bug against first-party code *today*. Two of them are downcall signature changes that
become unpayable MAJOR breaks the moment one external plugin exists — which is why they are first.

- **`main.py`** — `create_classes` catches only `AttributeError`, `ModuleNotFoundError` and
  `TypeError`. A module raising a bare `ImportError` (a *parent* of `ModuleNotFoundError`, so not
  caught), a `RuntimeError`, a `SyntaxError` or a `FileNotFoundError` at import time **takes the whole
  EMS down** — reproduced, exit 1, with a healthy second connector never constructed. Devices are
  built first, inside a `try/finally` with no `except`, so one bad device module means no connector is
  ever created at all. Add, after the existing clauses and keeping their distinct wording:
  - `except SystemExit` → error naming that plugins must return, not exit; skip the entry.
  - `except Exception` → `LOGGER.exception(...)`; skip the entry.

  **Not** `BaseException` wholesale: `KeyboardInterrupt` during startup is an operator action and must
  still terminate. `SystemExit` + `Exception` covers every failure reproduced, without swallowing Ctrl-C.
- **`main.py`** — fold `module.__file__` into the existing INFO "created" line. A silently shadowed
  plugin and a silently skipped entry are the two failures an operator cannot debug from the logs.
- **`supervisor/supervisor.py`** — `self._finished.set()` sits after the loop with no `finally`, and
  `_run` catches only `Exception`. A worker calling `sys.exit()` escapes, `_finished` is never set, and
  `main`'s `all(worker.is_finished())` polls a worker that will never finish — **the EMS neither
  restarts nor exits until SIGTERM**. Wrap the whole loop in `try: … finally: self._finished.set()`,
  with a distinct branch logging a worker that exited. *(The comment at `:73` already claims
  `_finished` is set on every exit path; the code does not deliver it.)*
- **`api/device.py` / `api/algorithm.py`** — `Device.control` logs and returns when the device is not
  writable, and `Algorithm.control_device` writes the decision to storage **unconditionally**. A
  read-only device claiming `Switch` therefore puts a false row into `algorithm_decisions.csv` every
  tick — into the versioned contract the viewer reads. Make `control` return `bool`, propagate it
  through `DevicesAccess`/`DevicesManager`, and write the decision only when the device accepted.
- **Tests** — a module raising `ImportError`, a constructor raising `ValueError`, a module with a
  `SyntaxError` and a module-top `sys.exit()` each produce one skipped entry; a `SystemExit` target
  leaves `is_finished()` true; a non-writable device claiming `Switch` produces no decision row.

> **Already in flight.** The uncommitted work in the tree adds `Connector.deliver()`, which guards the
> device plugin boundary with a broad catch and is adopted by all eight connectors — including the
> MQTT per-message daemon thread, which previously lost a raise to `threading.excepthook`. That is the
> same doctrine as this commit, applied at the *read* boundary. This commit applies it at the three
> boundaries `deliver()` does not cover: **import time**, **supervision**, and **the control path**.

*Rollback: ordinary revert. Mentions no ecosystem.*

### Commit 2 — "A namespaced plugin's options are validated like everything else"

- The two-line fix from §2, **together**, plus a rewrite of the comment at `config/config.py:24-27`
  which currently states that dotted paths are refused. Keep the traversal-guard reasoning intact.
- Add the fifth axis: `_validate_plugin_options(config.get("algorithms", []), "algorithms", "class", "Algorithm")`,
  and ship `algorithms/auto_toggle.schema.json` and `algorithms/device_checker.schema.json`.
- `tests/test_config.py` — add `algorithms` to the `AXES` parametrisation; extend the injected-handle
  denylist so an algorithm schema may not declare `devices_manager`; add a dotted-name-is-validated
  case and a dotted-name-with-no-schema-is-silent case; keep the existing traversal test green.
- `CONTRIBUTING.md` (algorithm recipe) — the "(the other three axes do)" parenthetical was already
  wrong before this commit: four other axes shipped schemas, `services` included. The whole sentence
  was replaced with a "Ship `algorithms/<class>.schema.json`" step, matching the other four recipes.

*Rollback: ordinary revert. Nothing external depends on it yet.*

### Commit 3 — "The harness is importable" (internal refactor, **not** a public API)

There is a contradiction shipping today: `CONTRIBUTING.md`'s five recipes tell authors to copy
`tests/test_mqtt_connector.py`, `tests/test_auto_toggle.py` and others — and **31 files under `tests/`
do `from tests.conftest import …`**, which does not resolve outside this checkout. The project's own
written advice does not work for the audience it is written for.

- **`api/testing.py`** — move the axis-generic doubles and threading harness out of `tests/conftest.py`
  (the stub device/connector/backend/algorithm, the devices-access factory, `run_in_thread`,
  `wait_until`, `assert_stops`, `write_replay`). `conftest.py` re-exports, so the suite is unchanged.
- **`api/conformance.py`** — lift the schema-validity check, the kwargs lockstep and the
  optional-dependency-vs-missing-plugin discrimination out of `tests/test_config.py`'s
  `TestShippedSchemas`, taking a directory and a target as arguments instead of deriving them from
  `__file__`. Add the two inverse checks the in-repo test omits (every parameter without a default
  appears in `required`; every parameter minus injected handles appears in `properties`), and the
  `Switch`-honesty check currently hand-copied into four device test files.
- **Add the check nothing has today**: call the real `Main.create_classes` with a one-entry config and
  assert one instance came back. Nothing currently tests the class-name match or the `issubclass`
  gate — which is exactly how a plugin passes its own tests and is then rejected at startup.
- `TestShippedSchemas` becomes a thin caller, so the kit and the suite **cannot drift**: if the kit
  breaks, this repo's `pytest` goes red.
- The commit message says explicitly that this is not yet a supported external API.

*Rollback: it is a refactor with the existing suite as its regression test.*

### Commit 4 — "The convention, in writing" (**the deliberate one-way door**)

This is the commit that creates an obligation. Everything in it is prose, `.gitignore` and
`.dockerignore`.

- **`CONTRIBUTING.md`** — *append* `## Recipe: publish a plugin outside this repository` between the
  algorithm recipe and `## Contracts shared with the other axes`. **Do not rename or reorder any
  existing heading**: the docs site links into this file by anchor, and a renamed heading 404s a
  published page. Content: the layout, the frozen config value from §4, the repo naming convention
  `motrix-edge-<axis>-<name>`, an extensionless `LICENSE` (the `.dockerignore` `*.md` rule would strip
  a `LICENSE.md` from a locally built image), and **the ceiling from §6, stated plainly**.
- **`CONTRIBUTING.md` — the admission policy**, one paragraph, and the piece that actually does the
  work §3 describes: *a plugin enters this repository only if it is a protocol rather than a vendor,
  has an openly published specification, and can be tested with no hardware and no network. Anything
  specific to one manufacturer's product lives in its author's own repository, on the convention
  above.* This is what caps in-tree growth; without it the split-the-repository question returns
  whatever the distribution mechanism, because the pressure is social rather than technical. With it,
  the answer to "will you merge my inverter connector?" is written down before it is first asked.

  **Settle one wrinkle while writing it**, rather than shipping a policy the tree contradicts: the
  criteria fit the *transport* connectors exactly, and fit `devices/` badly. `home_assistant` and
  `openems` are platforms rather than protocols, though both have openly published APIs and are tested
  with no hardware, so they pass on the other two criteria. `devices/shelly_plug.py` is the genuine
  exception — one manufacturer's product, in-tree today. Devices are inherently product-shaped, which
  is the whole point of the axis, so the policy needs either per-axis wording (protocol-or-nothing for
  `connectors/`, a looser bar for `devices/`) or an explicit grandfather clause. Do not word it as a
  description of current practice: one shipped device would falsify that on the day it merges.
- **`CONTRIBUTING.md`** — a `## Licensing` paragraph: Apache-2.0, inbound = outbound, no CLA, no
  copyright assignment. There is currently no mention of licence, copyright, DCO or sign-off anywhere
  in the file.
- **`SECURITY.md`** — today's scope says "a plugin **you wrote**" and "**your own** config.json". That
  sentence stops being true the day this merges. Keep "there is no plugin sandbox and there is not
  meant to be one"; drop the ownership qualifiers; add a `## Third-party plugins` section: not covered
  by Supported versions, report to the plugin's own repository first, and **the remedy available to
  these maintainers is delisting and a note, not a patch**. State what a plugin actually reaches — every
  device through the `DevicesManager` singleton whether or not it was injected one, and a connector's
  credentials through a device's deliberately-shared live connector.
- **`.gitignore`** — `/connectors/*/`, `/devices/*/`, `/algorithms/*/`, `/storage/*/`, `/services/*/`
  (no shipped axis has sub-directories today), so `git status` stays clean and `git pull` never
  conflicts with an operator's plugins.
- **`.dockerignore`** — add `**/.git`. The bare `.git` line matches only the context root, so a cloned
  plugin's `.git` would otherwise be baked into every locally built image.
- **The Docker paragraph, and it is shorter than everyone expects**: compose builds `edge` from
  `context: .`, and `.dockerignore` excludes neither `connectors/` nor its sub-directories — so
  `docker compose up -d --build` **already picks up a vendored plugin**. No compose edit, no bind
  mount, no `PYTHONPATH`, no derived image. There is no published `motrix-edge` image today (the only
  `ghcr.io` reference in the repo is the viewer's), so rebuilding is the status quo, not a new
  imposition. The only real gap is a plugin with its own pip dependency — see triggered option 1.
- **Discovery, free**: a GitHub topic `motrix-edge-plugin`, the repo naming convention, one sentence
  in `README.md`. No index, no table, no tiers.
- ~~**One template repository**~~ — **built, then deliberately deleted.** It existed for an afternoon
  and did not survive the first question asked of it: this repository already ships 25 worked plugins
  that the suite keeps green against a moving runtime, the recipes already route an author to the one
  whose shape matches, and a template repository has no CI against `main` — so it rots into teaching a
  layout that no longer loads. It was also an *untriggered* option shipped because this bullet listed
  it, which is the mistake the triggered-options table exists to prevent.

  What it uniquely carried was the out-of-tree scaffolding, which no in-tree plugin can demonstrate
  because none of them needs it. That was salvaged rather than lost: the `conftest.py` and the CI job
  are written out on the docs site's publishing page, and `tests/test_published_plugin.py` is the
  executable proof — it builds a vendor package in a temp directory and drives the real loader through
  it on three axes, in the repository that is already maintained. Writing it surfaced a trap the
  template had shipped undocumented: extending a package's `__path__` merges *imports* but not
  *resources*, so `files()` never sees an out-of-tree schema — the same flaw §9 rejects
  `pkgutil.extend_path` for.

  Revisit when two authors have hit the same scaffolding problem. Original plan: the skeleton, an
  extensionless `LICENSE`, and a CI workflow copying this
  repo's security posture verbatim (`permissions: contents: read`, SHA-pinned actions,
  `persist-credentials: false`).
- `.github/ISSUE_TEMPLATE/config.yml` — a contact link routing community-plugin problems off this
  tracker.

*Rollback: docs plus two ignore files — an afternoon. That reversibility is the strongest argument for
this ordering, and it is why the config value in §4 is the only thing being frozen.*

### Triggered options — entry conditions, not a schedule

| # | Trigger | What ships |
|---|---|---|
| 1 | A plugin needs a pip dependency **and** its operator runs compose | A tolerant-glob build layer: `COPY requirements.txt requirements-api.txt plugins.tx[t] ./` plus `if [ -f plugins.txt ]`. Byte-identical build when absent. Put the "everything here runs in-process with full access to every device and every credential" sentence *inside* `plugins.txt` as a comment, where it is read at the moment of decision. |
| 2 | Three plugins exist, or someone asks where to find one | A `## Community plugins` section on the **existing** docs-site `reference/plugin-catalog.md`: Name / Repository / Author / SPDX licence / Config value / Tested against, plus six lines of listing policy. No second repository, no tiers, no audit cron. |
| 3 | An author asks what they are compatible with, **or** the first `api/` change breaks a published plugin | `api/version.py` with `PLUGIN_API_VERSION`, mirroring `config/version.py`'s `Compatibility`/`compare` shape and its doctrine exactly: absence is not a claim, nothing is fatal, and a verdict nobody can act on earns no message. Write the bump rule down: a defaulted parameter on an **upcall** is MINOR (this is precisely why `on_device_data_received` gained `accepted` with a default); on a **downcall** it is MAJOR, because the framework calls plugin methods positionally. Retrofit-safe by construction — which is why it is not a prerequisite. |
| 4 | A listed repository changes hands, an operator reports bytes they did not expect, or the table passes ~8 rows | Content hashes, cheapest form first: a `sha256` column and a documented verification step. A lockfile or a CLI only if that proves insufficient. |
| 5 | Two plugins need to depend on each other **and** three need pip dependency resolution | Only then consider `pyproject.toml`. If taken, a `motrix_edge.` rename is **mandatory in the same change** — publishing `api`, `config`, `connectors`, `devices`, `storage`, `services` as flat top-level names is a land-grab and widens the loader's import surface. The config value frozen in §4 survives it unchanged. |
| 6 | A concrete plugin that genuinely cannot live under `<axis>/<vendor>/` | Only then delete the five axis `__init__.py`, and ship the startup `__path__` assertion (**abort**, not error — a run that comes up healthy-looking with every built-in connector erased is worse than one that refuses to start), the CI guard and the regression test **in the same commit**. Until then, keeping them regular is worth more than spending them. |
| 7 | None — runs in parallel at any time, zero cost | A curated `profiles/` directory of reviewable JSON register-map and payload-map fragments, retrieved by copy-paste. **No `profile` config key, no resolver, no format version, and no `CONFIG_FORMAT_VERSION` bump.** Promote to a real `profile` key only when three or more profiles exist *and* copy-paste has demonstrably failed someone. |

**Never, absent a maintainer with capacity to lose:** a `verified` tier (it is an endorsement a small
team cannot back and cannot cheaply withdraw — and no review can establish safety, because the loader
executes module-level code *before* the `issubclass` gate), a scheduled job that imports third-party
code on org runners, an in-app installer, a runtime fetch during `Config` construction (it is built in
`Main.__init__`, before `main()`'s `try/finally`, with no shutdown path to raise into), or any index
the EMS itself reads.

---

## 8. The two stories, end to end

**Publishing a connector** — after commit 4:

```
motrix-edge-connector-solarvendor/
	connectors/acme/solar.py
	connectors/acme/solar.schema.json
	tests/test_solar.py          # imports api.testing, runs api.conformance
	LICENSE                      # extensionless
	README.md
```

Tag it. Add the `motrix-edge-plugin` GitHub topic. That is the whole process: no PyPI account, no
rename, no packaging vocabulary, no waiting for a maintainer.

**Retrieving it** — three commands and one config entry:

```bash
git clone --depth 1 https://github.com/someone/motrix-edge-connector-solarvendor /tmp/p
cp -r /tmp/p/connectors/acme connectors/
docker compose up -d --build
```

```json
{ "name": "roof", "protocol": "acme.solar", "options": { "host": "10.0.0.7" } }
```

Bad options are a startup warning naming the key. A missing optional dependency is one skipped entry
and the rest of the EMS starts. A module that raises at import is — after commit 1 — one skipped entry
and a traceback.

---

## 9. Rejected, and why

- **Splitting the shipped connectors out into separate repositories.** See §3: the whole set is 162 KB,
  an unconfigured plugin is never imported, and the split would cost atomic refactor while requiring
  the PyPI work in option 5 first. The growth it is meant to control is capped by the admission policy
  instead.
- **PyPI distributions + entry points.** Verified blocker: a pip-installed plugin cannot
  `from api.connector import Connector` — there is no `pyproject.toml`, so `motrix-edge` cannot even be
  declared as a dependency. Making it installable means claiming `api`, `config`, `connectors`,
  `devices`, `storage`, `services` as flat top-level PyPI names, or a ~218-line import rewrite across
  76 files. It is also circularly gated: nobody can publish until the runtime is on PyPI, and the
  runtime should not go to PyPI until plugins exist. → **option 5**.
- **Deleting the axis `__init__.py` for PEP 420.** Verified to work (all 1213 tests still pass), and
  verified to open the hijack hazard in §2. It buys nothing the nested layout does not already give.
  → **option 6**.
- **`pkgutil.extend_path` as a gentler alternative.** A trap: imports merge, but
  `importlib.resources.files()` still returns the first portion only, so out-of-tree schemas become
  invisible and **option validation is silently lost**. Rejected outright.
- **An index-first store with tiers, governance and an audit cron.** Maximum support obligation before
  a single plugin exists. An index of three entries is worse than a GitHub topic. → **option 2/4**.
- **A `profile` config key in the first code phase.** It spends a `CONFIG_FORMAT_VERSION` minor on a
  mechanism serving only the Modbus/LoRa slice, and it is not reversible. The content idea is right;
  the mechanism is premature. → **option 7**.

---

## 10. What an operator is actually agreeing to

Say it in `SECURITY.md`, not in a footnote. A community plugin runs **in-process**, in a supervised
thread, and there is no sandbox — Python offers none worth the name. It can actuate any physical
device, read every other device's live state through the `DevicesManager` singleton whether or not one
was injected into it, reach a connector's credentials through a device's deliberately-shared live
connector (which is why `services/rest_api.py` allowlists every field it serves instead of using
`vars()`), and write anything it likes to storage.

Commit 1 does not change that. What it changes is the *blast radius of a mistake*: one bad plugin
becomes one skipped entry instead of a dead EMS. Those are different problems and the plan should
never let them be confused — **containment is for bugs; for malice there is only provenance**, which is
what option 4's content hashes buy and what a `verified` badge falsely appears to buy.

One security note for the data path, which applies whether or not a resolver is ever built: **a shared
config fragment must never contain `${`.** A profile describes hardware, not a deployment — and
`Config._interpolate` resolves `${VAR}` anywhere in the document, so a contributed fragment setting
`"unit": "${MQTT_PASSWORD}"` rides a credential into `device_data.csv`. Refuse the string in the
reviewer checklist from day one.

---

## 11. Cross-repo consequences

- **`architecture/plugins.md` on the docs site** opens with "no plugin registry, no entry-point metadata
  and no central import list… this page is the complete description of it". Commit 4 falsifies the
  *completeness*, not the mechanism. Amend it **in the same commit**: it remains the complete
  description of how a plugin **loads**; where a plugin may come **from** moves to a new page. Do not
  let that sentence quietly become false.
- **`reference/plugin-catalog.md`** is the right seed for option 2 — it is already maintained and
  already enforced by the PR checklist. It should gain a section, not a sibling repository.
- **Anchors are a contract.** The docs site links into `CONTRIBUTING.md` headings by anchor. New
  sections are *appended between* existing ones; a heading change requires a matching PR in
  `Motrix-Energy.github.io`.
- **The viewer** is unaffected by third-party *devices*. It is exposed to third-party *storage
  backends* — see §5.

---

## Appendix A — what was actually run

Executed on CPython 3.13.14 and cross-checked on 3.12.0, against copies; the repository was not modified.

| Claim | Result |
|---|---|
| `connectors/<vendor>/<name>.py` imports under the shipped regular `__init__.py`, and the loader derives and matches the class | **confirmed** — `connectors.__path__` unchanged, `SolarConnector` matched |
| Its schema is found today | **refuted** — `joinpath("acme.solar.schema.json")` is False, and the regex rejects the dotted name first, with no diagnostic at all |
| Both fixes together find it | **confirmed** — `joinpath("acme/solar.schema.json")` is True; widened regex accepts `acme.solar` and still rejects `../etc/passwd`, `acme/foo`, `/abs`, `.hidden`, `a..b` |
| Deleting the five axis `__init__.py` works and breaks nothing | **confirmed** — out-of-tree plugin loads and validates; 1213 tests pass before and after |
| …and opens a hijack | **confirmed** — a portion shipping `connectors/__init__.py` collapses `__path__` to itself *even when later on `sys.path`*, erasing every built-in connector |
| Built-ins can be shadowed by a third party | **refuted** — the checkout is `sys.path[0]` (the *script's* directory) and always wins. The real hazard is the reverse: a colliding third-party module is silently unreachable, with no warning |
| `pkgutil.extend_path` is equivalent | **refuted** — imports merge, resources do not; schema validation is silently lost |
| `ModuleNotFoundError` is a subclass of `ImportError` | **confirmed** — so a bare `ImportError` is *not* caught by the loader |
| A plugin raising at import kills the EMS | **confirmed** — exit 1; a healthy second connector never constructed |
| `supervisor` sets `_finished` on every exit path | **refuted** — it is after the loop, not in a `finally`, and `_run` catches only `Exception` |
| A pip-installed plugin can `from api.connector import …` | **refuted** — `ModuleNotFoundError: No module named 'api'` |
| `role` is a rich vocabulary | **refuted** — closed enum, one member, `energy_import_kwh` |
| Supporting an unknown LoRaWAN server costs zero Python | **confirmed** — `examples/connectors/lorawan.json`, `profile: "custom"` |

## Appendix B — open questions

1. **Is the bet right?** This plan bets the first community contribution is Python (a connector or an
   algorithm), and puts data-sharing in a parallel, zero-cost track. If you believe it will be a Modbus
   register map, options 7 and 2 move to the front and commit 4's recipe leads with data.
2. **The capability vocabulary (§6)** is the real ceiling on shared algorithms. Widening it — power,
   state of charge, setpoint, price, forecast — is a domain-model decision, and it is worth more to
   algorithm sharing than anything else in this document. Should it be scheduled *before* the
   publishing recipe, so the recipe does not advertise a ceiling it is about to raise?
3. **`services/` cannot be written against a published surface** (§5). Leave it out of the recipe, or
   introduce a narrow injected-handle protocol so it can be shared?
4. **`algorithms/` schemas** (commit 2) add validation to the one axis whose recipe currently says the
   constructor signature is the whole contract. That is an improvement, but it is also a doctrine
   change — confirm it is wanted.
