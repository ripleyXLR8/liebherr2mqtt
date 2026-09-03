<h1 align="center">
  <img src="liebherr2mqtt-icon.png" width="120" alt="liebherr2mqtt"><br>
  liebherr2mqtt
</h1>

<p align="center">
  <b>Liebherr SmartDevice HomeAPI → MQTT bridge, with Home Assistant style discovery.</b><br>
  Brings a connected Liebherr fridge or freezer into <b>Jeedom</b>, <b>Home Assistant</b>,
  or anything else that speaks MQTT Discovery — in real time, without polling the cloud.
</p>

---

## Why

Liebherr appliances fitted with a **SmartDeviceBox** are cloud-only in practice. The box does
contain a *LocalAPI*, but Liebherr disables it remotely on consumer appliances — it is only
enabled on the SmartModule of their professional ranges. The remaining option is the official
**SmartDevice HomeAPI**, published in beta in April 2025.

Home Assistant has had a first-class integration for it since 2026.3. Jeedom has nothing.
This bridge fills that gap, and works for any MQTT-Discovery consumer.

It is built on **[`pyliebherrhomeapi`](https://pypi.org/project/pyliebherrhomeapi/)**, the library
behind Home Assistant's official `liebherr` integration, rather than on a hand-rolled HTTP client —
so it inherits that project's data model, error taxonomy and SSE reconnection logic.

## Features

- **Real-time, no polling.** State changes arrive as a push over **Server-Sent Events**
  (`/v1/sse/devices/{id}/controls`). Liebherr rate-limits the HomeAPI and has been known to block
  calling IP addresses; this bridge simply does not hammer it. A single full re-read every 15
  minutes (configurable, and disableable) acts as a safety net.
- **Auto-discovery.** Entities appear on their own in Jeedom or Home Assistant, grouped as one
  device, with proper units, ranges and icons.
- **Reconnection-proof.** Discovery, states **and command subscriptions** are replayed on *every*
  MQTT (re)connection. This is the failure mode that silently breaks a lot of home-made bridges:
  with a non-persistent session the broker drops your subscriptions on any disconnect, and a bridge
  that only subscribes at startup keeps publishing states while quietly ignoring every command.
- **Last Will.** If the container dies, the appliance's *online* indicator flips to off by itself.
- **Only what your appliance actually has.** Entities are built from the controls the device
  declares, not from a hard-coded model list.

## Supported controls

| HomeAPI control | Published as | Notes |
|---|---|---|
| `temperature` | `sensor` (measured) + `number` (setpoint) | Uses the device's own min/max. Becomes a `select` if the appliance only accepts discrete steps. |
| `supercool` | `switch` | Per zone |
| `superfrost` | `switch` | Per zone |
| `partymode` | `switch` | Whole appliance. ⚠️ Runs SuperCool/SuperFrost for 24 h |
| `nightmode` | `switch` | Whole appliance |
| `holidaymode`, `bottletimer` | `switch` | Whole appliance |
| `presentationlight` | `number` | Brightness, 0…max |
| `icemaker` | `select` | Off / On / MaxIce, MaxIce only if supported |
| `hydrobreeze` | `select` | Off / Low / Medium / High |
| `biofreshplus` | `select` | Only the modes the appliance reports as supported |
| `autodoor` | `sensor` (state) + `switch` (open) | |
| — | `binary_sensor` "online" | Connectivity, backed by the MQTT Last Will |

Multi-zone appliances get one entity per zone, suffixed with the zone position.

> ❌ **Door state and door alarms are not available.** They are not part of the HomeAPI — the
> mobile app reads them from a separate, undocumented internal API.

## Getting an API key

In the **SmartDevice** mobile app:

> Settings → *Become a beta tester* → *Activate the HomeAPI interface* → **Generate a new key**

⚠️ **The key can only be copied once.** Copy it before leaving that screen.

## Running it

### Docker

```bash
docker run -d \
  --name liebherr2mqtt \
  --restart unless-stopped \
  -e LIEBHERR_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  -e MQTT_HOST=192.168.1.10 \
  -e MQTT_USER=mqttuser \
  -e MQTT_PASSWORD=mqttpassword \
  ghcr.io/ripleyxlr8/liebherr2mqtt:latest
```

### Unraid

Install it from **Community Applications** — open the *Apps* tab and search for `liebherr2mqtt`.
Every setting is a field in the UI, and the API key field is masked.

The CA template lives in
[ripleyXLR8/unraid-templates](https://github.com/ripleyXLR8/unraid-templates/blob/main/liebherr2mqtt.xml),
which is the repository Community Applications indexes.

### Configuration

Everything can be set through **environment variables**, or through a
`/config/liebherr2mqtt.conf` file (see [`liebherr2mqtt.conf.template`](liebherr2mqtt.conf.template)).
Environment variables win over the file, and the file is entirely optional.

| Variable | Default | Meaning |
|---|---|---|
| `LIEBHERR_API_KEY` | — | **Required.** HomeAPI key from the app |
| `LIEBHERR_REFRESH_INTERVAL` | `900` | Seconds between full re-reads; `0` disables |
| `MQTT_HOST` / `MQTT_PORT` | `127.0.0.1` / `1883` | Broker |
| `MQTT_USER` / `MQTT_PASSWORD` | empty | Leave empty for an anonymous broker |
| `MQTT_CLIENT_ID` | `liebherr2mqtt` | |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Where discovery messages go |
| `MQTT_TOPIC_PREFIX` | `liebherr` | Root of state and command topics |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Topics

```
liebherr/<device>/availability              online | offline
liebherr/<device>/<entity>/state            current value    (retained)
liebherr/<device>/<entity>/set              command
homeassistant/<component>/liebherr_<device>/<entity>/config   discovery (retained)
```

## Jeedom notes

Install the **MQTT Discovery** plugin, point it at your broker, and the appliance shows up on its
own. Two traps worth knowing:

- ⚠️ **Add `liebherr` to the plugin's "data topics" setting.** The plugin checks that the root of a
  discovery message's state topic is in that list, and **silently ignores** the message otherwise —
  it merely records the unknown root in `discovered_data_topics`. Nothing appears, and nothing
  explains why.
- The plugin **renames a command after the discovery `device_class`**, which is why the setpoint
  here deliberately carries no `device_class`: it would be created as a second command named
  "Temperature" instead of "Setpoint".

## Experimental: mobile-API mode (alarms)

> **This section only exists on the `mobile-api` branch.** It is off by default
> and does not affect normal operation. Do not merge it to `main` without a
> deliberate decision — it relies on Liebherr's **undocumented internal mobile
> API**, which can change without notice, and replaying the app's OAuth client
> is more sensitive towards Liebherr's terms than the official HomeAPI key. It
> runs on the account whose credentials complete the login.

The documented HomeAPI does **not** expose appliance alarms. The official app
gets them from a separate internal API (`mobile-api.smartdevice.liebherr.com`),
authenticated with an OAuth token from `login.liebherr.com`. When enabled, this
mode logs in once, then polls `/v1/household/notifications` and publishes a few
extra entities **under the same device**:

| Entity | Type | Meaning |
|---|---|---|
| Alarme porte | binary_sensor | Door-left-open alarm active |
| Alarme température | binary_sensor | Temperature alarm active (high or low) |
| Coupure de courant | binary_sensor | Power-failure alarm |
| Rappel filtre à air | binary_sensor | Air-filter reminder |
| Autre alarme | binary_sensor | Any other/unknown notification type |
| Dernière notification | sensor | Most recent notification (type + timestamp) |

> These are alarm **events** (e.g. "door has been open too long"), not a live
> door open/closed state — the API has no such field. For continuous door
> state, a Zigbee door sensor is still the better tool.

### Enabling it

**1. Get a `:mobile-api` image.** Pushing this branch builds
`ghcr.io/ripleyxlr8/liebherr2mqtt:mobile-api` (multi-arch), which stays separate
from `:latest`.

**2. One-off login.** The OAuth flow needs an interactive login in a browser
_you_ control; the bridge never sees your Liebherr password.

```bash
docker run -it --rm \
  -v /mnt/user/appdata/liebherr2mqtt:/config \
  ghcr.io/ripleyxlr8/liebherr2mqtt:mobile-api auth
```

It prints an authorization URL. Open it, log in on `login.liebherr.com`, and the
browser will try to open a `smartdevice://auth?code=...` URL that does not
resolve (it is the app's private scheme) — copy that whole URL from the address
bar and paste it back at the prompt. The token, including a refresh token, is
written to `/config/liebherr_mobile_token.json` and the login is not needed
again.

**3. Turn the mode on.** Set `MOBILE_API_ENABLED=true` (or `enabled = true`
under `[mobile]` in the config file) and restart the container normally. The
HomeAPI path keeps working exactly as before; the alarm entities appear
alongside it.

| Variable | Default | Meaning |
|---|---|---|
| `MOBILE_API_ENABLED` | `false` | Master switch for this mode |
| `MOBILE_POLL_INTERVAL` | `300` | Seconds between notification polls |
| `MOBILE_TOKEN_FILE` | `/config/liebherr_mobile_token.json` | OAuth token store |
| `MOBILE_AUTH_BASE` / `MOBILE_API_BASE` | login / mobile-api hosts | Override for staging |
| `MOBILE_CLIENT_ID` / `MOBILE_REDIRECT_URI` / `MOBILE_SCOPE` | app defaults | Rarely changed |

If the mode is enabled but no token exists yet, the bridge logs a warning and
carries on in normal mode — it never blocks on the mobile side.

## Home Assistant notes

If you use Home Assistant, prefer its
**[official `liebherr` integration](https://www.home-assistant.io/integrations/liebherr/)** — it
talks to the same API directly, with no broker in between. This bridge is for everything else.

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with, endorsed by, or supported by Liebherr. "Liebherr", "SmartDevice",
"BioFresh", "SuperCool" and "SuperFrost" are trademarks of their respective owners.
