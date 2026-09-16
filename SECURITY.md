# Security policy

## Reporting a vulnerability

**Do not open a public issue.** Use GitHub's private vulnerability reporting on this repository — the
**Security** tab → *Report a vulnerability*. It stays private to the maintainers and gives us
somewhere to work on a fix with you before anything is public.

Include the commit or image tag, how you were running it (`python main.py`, `docker compose`, which
profiles), and the smallest configuration that reproduces it. **Redact your `config.json` and your
logs** before attaching them — hostnames, topics, serials, addresses, anything from a real site. The
standing privacy rule applies to security reports as much as to pull requests, and the shipped sample
vocabulary (`p1_meter`, `shelly_plug`, `pseudo_sensor`, `AutoToggle`) usually reproduces the same
thing.

Expect an acknowledgement within a few days. This is a small project with no on-call: if something is
being actively exploited against you, say so in the first line.

## Supported versions

`main`, and the most recent tag. Fixes go out in the next tag; there are no backports.

## The model, before you report

Motrix Edge ships **no authentication of its own, deliberately**. `services/rest_api.py` serves live
device data and site topology unauthenticated, and that is safe for exactly one reason: nothing
outside the compose network can reach it. The one gate in the system is nginx's, in the *viewer*
container, and it gates `location /api/` only. One gate in one place is a model you can audit; a
second, half-hearted gate inside the EMS would be a second password store, a second CORS policy and a
second thing to misconfigure.

The corollary is the operational rule that holds the whole model up: `docker-compose.yml` keeps the
`ports:` block on `edge` **commented out**. Uncommenting it puts an unauthenticated copy of every
route beside the gated one — and a bare `8000:8000` binds `0.0.0.0`, putting live device data and
site topology on the LAN. The service's own docstring names the trigger for changing the model:
publish that port, and authentication becomes the first thing to add.

## In scope

- Anything that reaches the API without the viewer's gate in the **shipped** compose topology.
- A response that serialises something it should not. Every payload names its fields explicitly —
  never `vars(obj)`, never an attribute walk — because a device snapshot deliberately shares the
  *live* connector object, so any generic serialisation would reach `MQTTConnector.password`. Device
  *data* is telemetry and may be exposed; device *options* are configuration and may hold
  credentials. A path that crosses that line is a real finding.
- A credential escaping into somewhere it is read: a log line, a traceback, a stored CSV or Influx
  point, an API field. `config.json` holds `${VAR}` references, never secrets, and `Config` resolves
  them after schema validation precisely so a committed config is always safe to commit.
- Anything in the plugin loader that imports or instantiates code outside the axis's package, or that
  can be steered into doing so by a config file.
- A payload from a device or broker that can crash the process unrecoverably, execute code, or
  traverse the filesystem through a parser or storage backend.
- Supply-chain problems in what we ship: a dependency pin pointing at something compromised, a
  workflow `uses:` that is not a full commit SHA, a job with more token scope than its steps need.

## Not vulnerabilities

Documented design decisions. An argument that one of them is *wrong* is welcome as a normal issue —
just not as a security report:

- **The REST API is unauthenticated.** By construction; see above.
- **`admin`/`admin` in `docker-compose.yml`.** A compose-side placeholder for the loopback
  quickstart, not an image default — the viewer image ships with an empty `.htpasswd` and refuses to
  start without `VIEWER_USER` and `VIEWER_PASSWORD`. Override both before the viewer leaves
  `127.0.0.1`.
- **Basic auth is reversibly encoded**, and the container says so at every start. Put TLS or a VPN in
  front before setting `VIEWER_BIND` to anything but loopback.
- **You published port 8000 yourself**, or set `VIEWER_BIND=0.0.0.0` on an untrusted network. That is
  an operator decision the compose file warns about in place.
- **The `mqtt` and `monitoring` profiles publish their own ports** (Mosquitto, InfluxDB, Grafana).
  They are development conveniences behind opt-in profiles, configured for a laptop, not for a site.
- **A plugin you wrote can do anything the process can.** Plugins are Python classes loaded from your
  own `config.json`; there is no sandbox and there is not meant to be one.

## Disclosure

We will confirm the report, agree a fix and a timeline with you, and credit you in the release notes
unless you would rather we did not. Please give us a reasonable window to ship before publishing.
