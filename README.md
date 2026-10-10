# RF Device with Learning

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

A Home Assistant integration that turns an **RF ceiling-fan remote** into proper
Home Assistant entities — by **learning** the remote's codes with a Broadlink
RM Pro / RM4 Pro and replaying them through Home Assistant's native
`radio_frequency` platform.

No code lists, no `SmartIR` JSON, no template fans. You point the wizard at your
Broadlink, press the buttons on your remote when prompted, and you get a real
**fan entity with speeds**, an optional **light**, and any **extra buttons** you
want — all from the UI.

## Why

Home Assistant's Broadlink integration can learn IR and act as a `remote`, but it
doesn't give you a *fan* with speed control. This integration does:

- A guided **"learn your remote" wizard** — define how many speeds your fan has,
  whether it has a separate On button, a light, and any custom buttons, then learn
  each one by pressing it on the remote.
- Creates an **optimistic `fan` entity** (Off + N speeds, optional On), an optional
  **`light` entity**, and one **`button` entity** per custom code.
- **Follows the physical remote**: the Broadlink listens in the background, so
  using the remote updates the entities in Home Assistant.
- **Re-learn or add buttons** any time from the entry's *Configure* screen.

## Requirements

- **Home Assistant 2026.10 or newer**. From 2026.10 the Broadlink integration uses
  the asynchronous `python-broadlink` 1.0 library, which this version needs. On
  Home Assistant 2026.5 to 2026.9, stay on version 1.1.0 of this integration.
- A **Broadlink RM Pro / RM4 Pro** (or other RF-capable Broadlink) already set up in
  the **Broadlink integration**. Its `radio_frequency` transmitter is what this
  integration sends through.
- An RF fan remote on a supported band (Broadlink covers **433 MHz** and **315 MHz**).

## Installation

### HACS (recommended)

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/clevrdavid/rf_fan` with category **Integration**.
3. Install **RF Device with Learning**, then **restart Home Assistant**.

*(Once accepted into the HACS default store, you'll be able to search for it
directly without adding the custom repository.)*

### Manual

Copy `custom_components/rf_fan/` into your Home Assistant `config/custom_components/`
directory and restart.

## Setup

1. **Settings → Devices & Services → Add Integration → RF Device with Learning.**
2. Give the fan a name and tell the wizard about its remote:
   - **Speed count** — how many discrete speeds (e.g. 3).
   - **Separate On button** — tick if the remote has a dedicated On (some only have
     speed buttons + Off).
   - **Has a light** — tick if the remote has a light toggle.
   - **Custom buttons** — a comma/newline-separated list of any extras (e.g.
     `Reverse, Timer`).
3. The wizard walks you through learning each button. **Press the button on the
   remote when prompted** — and learn each speed by the speed it *actually*
   produces, not the label printed on the remote (some remotes are mislabelled).
4. Finish, and your fan/light/button entities appear.

### Fans that won't learn with the sweep

Some remotes send very short bursts that Broadlink's frequency *sweep* can't lock
onto (e.g. the **Mercator FRM97**). Tick **"Capture without the sweep"** (direct
capture) and set the frequency (usually `433.92` MHz) — it listens at that
frequency directly, like learning an IR code. Directly-captured fans are
automatically transmitted as a cleaned, repeated frame so they trigger reliably.

### Re-learning

Open the integration entry → **Configure** to re-learn a single button or add a new
one without starting over.

## Following the physical remote

The Broadlink that transmits for a device also listens for that device's remote.
When it hears a learned code, the entity takes on the state that button leaves the
device in: a fan speed or Off, a light toggled (or switched on/off with separate
buttons), a cover opened or closed. Custom buttons have no state, so nothing
changes for them.

Listening is on by default. To turn it off for a device, open its entry, choose
**Configure → Follow the physical remote** and untick the option.

Things to know:

- Listening keeps the Broadlink's receiver in RF learning mode, polling it twice a
  second. While it runs, learning codes with the **Broadlink integration itself**
  (`remote.learn_command`, IR or RF) on the same Broadlink will not work reliably.
  Turn listening off for every device on that Broadlink first. Learning through
  this integration's wizard pauses listening on its own.
- A button heard again within 2 seconds counts as the same press, because a held
  button keeps repeating. Two quick presses of a light toggle therefore count once.
  The code Home Assistant has just sent is ignored the same way.
- The Broadlink listens at one frequency. If devices on the same Broadlink use
  different frequencies, only the first one set up is heard and a warning is logged.
- Only devices whose transmitter is a Broadlink are listened for.
- Remotes with rolling codes send a different code on every press, so they cannot
  be matched.

To see what the Broadlink hears and what it matched, enable debug logging for
`custom_components.rf_fan`.

## How it works

The `radio_frequency` platform is **send-only** — it has no learning. So this
integration:

1. **Captures** a code with the Broadlink's own RF learn.
2. **Decodes** the Broadlink pulse packet into raw OOK microsecond timings (the
   exact inverse of Broadlink's encoder).
3. **Transmits** it back through `radio_frequency.async_send_command` via a small
   `RadioFrequencyCommand` subclass carrying those timings — transmitter-agnostic,
   on Home Assistant's supported rails.
4. **Listens** through python-broadlink's open-ended RF capture, which re-arms the
   Broadlink when it drops out of learning mode and after every send. Each heard
   capture is split into its repeated frames and compared, pulse by pulse with a
   tolerance for receiver jitter, with a frame of each learned code.

Version 2.0 decodes with python-broadlink's corrected timing unit (about 30.45 us
instead of 32.84 us). Codes learned with 1.x are re-decoded from their stored
Broadlink packets on upgrade, so they replay with the timings the remote sent.

## Notes & limitations

- **RF is one-way**, so entities are **optimistic** (`assumed_state`) — Home
  Assistant tracks what it last sent and what the Broadlink last heard from the
  remote; it can't read the fan's actual state. Presses the Broadlink misses (out
  of range, during learning, or with listening off) leave the state behind. The
  discrete-speed + dedicated-Off design keeps this accurate in practice.
- Learning currently **requires a Broadlink** (it's the capture device). Transmit is
  transmitter-agnostic via the `radio_frequency` platform.
- Some AC fans run weak on low/medium speeds — that's the fan's speed **capacitor**
  (hardware), not the codes; this integration reproduces the remote faithfully.

## Development

The tests run Home Assistant 2026.10.0 against a simulated RM4 Pro and need
Python 3.14:

```
pip install -r requirements_test.txt
pytest
```

## License

[MIT](LICENSE)
