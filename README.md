# fibioslocation

Fetch iCloud Find My device locations (your own + family) and push them to a [Fibaro HC3](https://www.fibaro.com/) home automation hub.

## Install

```bash
pip install fibioslocation
```

## Usage

```bash
fibioslocation
fibioslocation --email you@icloud.com
fibioslocation --interval 60      # poll every 60 s instead of 3 min
fibioslocation --once             # single shot, then exit
fibioslocation --no-hc3           # display only, don't push to HC3
fibioslocation --debug            # diagnose 2FA options
```

## Credentials

Create `~/.env` with:

```
HC3_HOST=192.168.1.10
HC3_USER=admin
HC3_PASSWORD=secret
```

## Requirements

- Python ≥ 3.10
- `pyicloud`, `rich`, `requests`
