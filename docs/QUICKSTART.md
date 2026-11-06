# Setup guide: Kiln on a Crucible machine

This guide installs Kiln on a Strix Halo machine that runs the public
[Crucible](https://github.com/jaedync/crucible-strix-halo) engine. At the end:

- the gateway serves an OpenAI-compatible API on `127.0.0.1:8000`,
- the dashboard is on `http://127.0.0.1:7000/`,
- Kiln runs the Crucible server as the systemd user unit `kiln-crucible`, and reads its log lines from the journal.

| | |
|---|---|
| Machine | A Strix Halo machine on which steps 1 to 5 of the [Crucible setup guide](https://github.com/jaedync/crucible-strix-halo/blob/main/docs/QUICKSTART.md) are done. |
| System | Linux with systemd. The install steps are tested on Ubuntu 24.04, Fedora 43 and Arch Linux in containers, with a fake Crucible server and no GPU. |
| Software | Python 3.11 or newer, `git`, `curl` and [uv](https://docs.astral.sh/uv/). |
| Time | A few minutes, plus the time that the engine needs to load the weights. |
| Ports | 8000 (gateway), 7000 (dashboard), 8001 (live events, loopback only), 8790 (the engine, loopback only). |

To see the dashboard first, without a GPU: `uvx --from git+https://github.com/jaedync/crucible-kiln kiln demo`
([DEMO](DEMO.md)).

## For an LLM agent that follows this guide

- Do the steps in order. Each step ends with a **Check**. Run it and compare the result.
- If a check fails, look up the line in [When a step fails](#when-a-step-fails) and do what it says, once.
  If the check fails again, stop. Report the step, the command and its full output to the user.
- No step needs `sudo`, except the rack panel option at the end. Do not use `sudo` elsewhere.
- Step 4 starts the engine, which loads about 100 GB into memory. Ask the user first if other programs use
  the GPU or much memory.
- Do not change the kernel command line, the boot loader or the Crucible checkout.

## Step 0: check what must be there

Set two variables: the Crucible checkout and the pack. These are the paths of the Crucible setup guide;
change them if yours differ.

```sh
CRUCIBLE="$HOME/crucible-strix-halo"
CKPT="$CRUCIBLE/models/Qwen3.8-Flash-Next-exl3-4.05bpw_h6_ng6"
```

A new terminal does not keep these variables. Step 2 sets them again.

**Check:**

```sh
test -x "$CRUCIBLE/build/engine/crucible" && echo ok        # expect: ok
ls "$CKPT/config.json"                                        # expect: the path, no error
(cd "$CRUCIBLE" && .venv/bin/python -m server --version)      # expect: crucible 0.33.1 or newer
systemctl --user is-system-running                            # expect: running (degraded also works)
pgrep -af 'python -m server' || echo none                     # expect: none
```

If the last check prints a process, a server runs already. Stop it now. Kiln starts the server itself, on
the same port, and it reads the engine's lines only from the journal of its own unit.

```sh
kill PID    # the process ID that pgrep printed
```

## Step 1: install Kiln

Install uv if `uv --version` fails. This is the installer from the uv documentation:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then open a new terminal, or run `source "$HOME/.local/bin/env"`.

```sh
uv tool install git+https://github.com/jaedync/crucible-kiln
```

uv puts the `kiln` command in `~/.local/bin`. If the shell does not find `kiln`, run `uv tool update-shell`
and open a new terminal.

Fallback without uv: install Kiln in a virtual environment. Do not use `pip install --user`: Ubuntu 24.04 and
Arch Linux refuse it (PEP 668).

```sh
python3 -m venv ~/kiln-venv
~/kiln-venv/bin/pip install git+https://github.com/jaedync/crucible-kiln
```

Then use `~/kiln-venv/bin/kiln` in place of `kiln` in the steps below.

**Check:**

```sh
kiln --version      # expect: a version number
```

## Step 2: write the configuration

Set the two variables again (the same values as in step 0), then write the configuration:

```sh
CRUCIBLE="$HOME/crucible-strix-halo"
CKPT="$CRUCIBLE/models/Qwen3.8-Flash-Next-exl3-4.05bpw_h6_ng6"
kiln init --crucible "$CRUCIBLE" --pack "$CKPT" --ctx 262144 --slots 4
```

`--ctx` and `--slots` become the server's `--ctx` and `--streams`. Without them, `kiln init` uses the
server's defaults (32,768 tokens and 1 slot). The command asks no questions. It writes three files, then
enables and starts the two Kiln units (`--no-start` leaves them stopped):

| File | Content |
|---|---|
| `~/.config/kiln/kiln.toml` | The configuration, with comments. One engine profile, `crucible`. |
| `~/.config/systemd/user/kiln-gateway.service` | The gateway. |
| `~/.config/systemd/user/kiln-dash.service` | The dashboard, the ingest and the engine manager. |

Other options: `--port` (gateway, default 8000), `--dash-port` (default 7000), `--engine-port` (default
8790), `--lan` (bind every interface, see [Security](../README.md#security)) and `--force` (replace files
that exist). `kiln init` never replaces a changed file without `--force`.

`--force` replaces `kiln.toml` and the two unit files. Your edits in them are lost, for example a second
profile. After `kiln init --force`, an engine that runs keeps its old command. Run `kiln switch crucible`
to start it with the new command.

**Check:**

```sh
kiln check     # expect: a summary of kiln.toml with the profile crucible
kiln doctor    # expect: no FAIL line
```

Before the engine runs, `kiln doctor` shows `WARN` for `engine` and `engine log`. That is correct.

## Step 3: start the gateway and the dashboard

```sh
systemctl --user start kiln-gateway kiln-dash
loginctl enable-linger "$USER"
```

`enable-linger` keeps your user units running after you log out, and starts them at boot. If `kiln init`
started the two units already, the `start` command does nothing. `kiln init` prints the `loginctl` line
only when your user does not linger yet.

**Check:**

```sh
systemctl --user is-active kiln-gateway kiln-dash   # expect: active, two times
curl -s http://127.0.0.1:8000/health                # expect: JSON with "ok": true
```

Open http://127.0.0.1:7000/ in a browser on this machine. The engine card says that no engine runs.

## Step 4: start the engine

```sh
kiln switch crucible
```

The command prints `{"accepted": true}` and returns at once. Kiln writes the unit `kiln-crucible.service`
and starts it. The server loads the weights; this can take a few minutes. Kiln marks the engine ready after
a test generation. The dashboard's engine card shows each step.

**Check** (repeat the first command until it shows `ready`; if it shows `failed`, see
[When a step fails](#when-a-step-fails)):

```sh
curl -s http://127.0.0.1:8000/health | grep -o '"state": "[a-z]*"'   # expect: "state": "ready"
kiln doctor                                                           # expect: no FAIL line
```

To read the server's own output: `journalctl --user -u kiln-crucible -n 50`.

After a reboot, the gateway, the dashboard and the active engine start again by themselves. The gateway
sends requests to the engine only after it answers a test generation. After `kiln stop`, no engine starts
at boot.

## Step 5: send a request through the gateway

```sh
curl -sS http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"crucible","messages":[{"role":"user","content":"Say hello."}],"max_tokens":2048}'
```

**Check:** the reply is JSON with an answer in `choices[0].message.content`. The dashboard shows the request
in "Recent requests", and `kiln doctor` shows `PASS` for `engine log`.

A streamed request shows each token when it arrives:

```sh
curl -sSN http://127.0.0.1:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"crucible","stream":true,"messages":[{"role":"user","content":"Count to five."}],"max_tokens":2048}'
```

**Check:** the reply is a series of `data: {...}` lines. The last line is `data: [DONE]`.

To use Kiln from a client, set the base URL to `http://127.0.0.1:8000/v1`, the model to `crucible` or the
`model` value in `kiln.toml`, and any API key. `kiln init` makes the `model` value from the pack directory
name. It changes the name to lower case, and each run of characters that are not `a-z`, `0-9` or `.` to
one `-`. It removes `-` and `.` at the start and the end. To read the value, run `kiln check`, or
`curl -s http://127.0.0.1:8000/v1/models` while the engine is ready. To give a client a
label in the dashboard, send the header `X-Kiln-Client: NAME`. A request row shows a decode rate only when
the reply has 32 or more tokens.

## Option: run the engine as in the Crucible setup guide

`kiln init` writes a short server command. To use the options of step 6 of the Crucible setup guide (the
n-gram table on the SSD, the conversation cache, image input), edit the table `[profiles.crucible.native]` in
`~/.config/kiln/kiln.toml`. Change only `ExecStart` and `Environment`. Keep the other keys: without
`WorkingDirectory`, the server does not start (`No module named server`). `ExecStart` must stay
on one line. systemd replaces `%h` with your home directory. The table then looks like this:

```toml
[profiles.crucible.native]
ExecStart = "%h/crucible-strix-halo/.venv/bin/python -m server %h/crucible-strix-halo/models/Qwen3.8-Flash-Next-exl3-4.05bpw_h6_ng6 --engine %h/crucible-strix-halo/build/engine/crucible --port 8790 --streams 4 --ctx 262144 --prefill 4096 --conversation-cache-mib 9700 --host-reserve-mib 11264 --state-dir %h/.cache/crucible/states --state-disk-gib 200 --vision --clamp-max-tokens --default-thinking on --max-body-mib 16"
Environment = ["PATH=/opt/rocm/bin:/usr/local/bin:/usr/bin:/bin", "PYTHONUNBUFFERED=1", "CRUCIBLE_PLE_HOST=1"]
WorkingDirectory = "%h/crucible-strix-halo"
LimitMEMLOCK = "infinity"
```

Keep `context` and `slots` of the profile equal to `--ctx` and `--streams`. Kiln reads `--state-dir` and
`--state-disk-gib` from the command, for the disk part of the memory map. Then load the new command:

```sh
mkdir -p ~/.cache/crucible/states
systemctl --user restart kiln-gateway kiln-dash
kiln switch crucible
```

**Check:** the same as step 4.

## Option: a second pack, to switch between packs

Copy the two tables `[profiles.crucible]` and `[profiles.crucible.native]` in `kiln.toml`. In the copy,
change these values:

- the name of the tables (for example `[profiles.crucible-305]` and `[profiles.crucible-305.native]`);
- `label` and `model` (each profile needs its own `model` id; `served_id = "crucible"` stays);
- `port`, and the same port in the `--port` option of `ExecStart`;
- the path of the other pack in `ExecStart`.

Then check the file and load it:

```sh
kiln check
systemctl --user restart kiln-gateway kiln-dash
```

The engine card of the dashboard then lists both profiles. A switch stops one engine and starts the other;
only one runs at a time. Requests that arrive during a switch wait (at most 150 s) and then go to the new
engine. From the shell: `kiln switch crucible-305`.

## Option: use Kiln from another machine

The safe way is an SSH tunnel from the other machine. Kiln keeps its default addresses:

```sh
ssh -N -L 7000:127.0.0.1:7000 -L 8000:127.0.0.1:8000 you@strix-halo
```

Then open http://127.0.0.1:7000/ on the other machine.

To bind every interface instead, edit `~/.config/kiln/kiln.toml`:

- in `[gateway]`, change `listen = "127.0.0.1:8000"` to `listen = "*:8000"`;
- in `[dash]`, change `listen = "127.0.0.1:7000"` to `listen = "*:7000"`.

Then check the file and restart the two units:

```sh
kiln check
systemctl --user restart kiln-gateway kiln-dash
```

`kiln init --lan --force` writes the same addresses, but it replaces all of `kiln.toml`. It removes your
earlier edits, for example a second profile or a changed engine command.

The gateway and the dashboard have no login: do this only on a network that you trust. Every viewer
of the dashboard sees all clients' device names, addresses, labels and live streams. Only loopback can switch
engines. To let one Tailscale device switch engines, add its MagicDNS name to `[actions] allow_nodes`
(for example `["my-laptop"]` for a device of your own tailnet, or the full name).
The machine's host names, its `.local` name, its MagicDNS name and IP addresses work at once. To use another
host name (for example a name from your LAN DNS), add it to `[gateway] allowed_hosts` and
`[dash] allowed_hosts`. Without that, Kiln refuses the request with status 403. A browser app that calls the
gateway from its page also needs its origin in `[gateway] allowed_origins` (for example
`["http://chat.example:3000"]`). The gateway then sends the CORS headers that the browser needs.

Do not put Kiln behind a reverse proxy on the same machine (`tailscale serve` or `funnel`, Caddy, nginx):
loopback may switch and stop engines. Never expose Kiln or the engine to the internet. The
[Security](../README.md#security) section of the README has the details.

## Option: the rack panel kiosk

`/panel` is a page for a 1424 x 280 touch display. Any browser can open it. The kiosk runs it full screen on
tty1, with cage and Chromium in software rendering. It needs `cage`, Chromium (at `/usr/bin/chromium` or
`/usr/bin/chromium-browser`) and `curl`, a checkout of this repository, and `sudo` for one system unit. It is tested on one machine only,
with Arch Linux.

Install the packages:

- Arch Linux: `sudo pacman -S cage chromium`.
- Debian: `sudo apt install cage chromium`.
- Fedora: `sudo dnf install cage chromium`.
- Ubuntu 24.04: `sudo apt install cage`. Chromium is only a snap there (`/snap/bin/chromium`). The kiosk is
  not tested with the snap.

`install-kiosk` stops before it writes a file if it does not find cage, Chromium or `sudo`. To give other
paths, set `KILN_PANEL_CAGE` or `KILN_PANEL_CHROMIUM` before you run it.

```sh
git clone https://github.com/jaedync/crucible-kiln
crucible-kiln/deploy/panel/install-kiosk
sudo systemctl daemon-reload
sudo systemctl enable --now kiln-panel-kiosk
```

**Check:** `systemctl status kiln-panel-kiosk` shows `active (running)`, and the display shows the panel.

## Update and remove

Update:

```sh
uv tool upgrade crucible-kiln
systemctl --user restart kiln-gateway kiln-dash
```

If you installed Kiln in a virtual environment (the fallback of step 1), update it with pip:

```sh
~/kiln-venv/bin/pip install --upgrade --force-reinstall git+https://github.com/jaedync/crucible-kiln
systemctl --user restart kiln-gateway kiln-dash
```

A restart of the gateway or the dashboard does not stop the engine. A restart of the gateway ends the
requests that run through it at that moment.

Remove (the last line deletes the history):

```sh
kiln stop
systemctl --user disable --now kiln-gateway kiln-dash
rm ~/.config/systemd/user/kiln-gateway.service ~/.config/systemd/user/kiln-dash.service ~/.config/systemd/user/kiln-crucible*.service
systemctl --user daemon-reload
uv tool uninstall crucible-kiln
rm -r ~/.config/kiln ~/.local/share/kiln ~/.local/state/kiln
```

For the virtual environment install, use `~/kiln-venv/bin/kiln stop` in the first line. In place of
`uv tool uninstall crucible-kiln`, run `rm -r ~/kiln-venv`.

## When a step fails

`kiln doctor` prints one line for each check: `PASS`, `WARN` or `FAIL`, with a fix. It exits with 1 when a
check fails. A `WARN` does not stop Kiln, but a part of the dashboard stays empty.

| Check or message | Status | What to do |
|---|---|---|
| `python` | FAIL | Install Python 3.11 or newer. Then `uv tool install --force --python 3.13 git+https://github.com/jaedync/crucible-kiln`. |
| `aiohttp` | FAIL | Install Kiln again: `uv tool install --force git+https://github.com/jaedync/crucible-kiln`. |
| `config` | FAIL | Run `kiln init` (step 2), or fix the file that the line names and run `kiln check`. |
| `systemd --user` | FAIL | The user manager does not answer. Log in on a normal session (SSH or a console), or run `loginctl enable-linger "$USER"`. In a `su` or `sudo -u` shell, set `XDG_RUNTIME_DIR=/run/user/$(id -u)`. |
| `systemd --user` | WARN `degraded` | A user unit failed. Run `systemctl --user --failed` to see which one. |
| `journal` | FAIL | Your user cannot read the journal. Add your user to the `systemd-journal` group, then log in again. |
| `journal` | WARN, no entries | Set `Storage=persistent` in `/etc/systemd/journald.conf`, so the journal keeps the lines of user units. |
| `amdgpu` | WARN | The `amdgpu` driver is not loaded. The dashboard shows no GPU memory and no GPU busy figures. |
| `kernel` | WARN | Linux is older than 6.5. The memory map shows no page cache figures for the PLE table. |
| `gateway port 8000` or `dash port 7000` | WARN, `systemd --user does not answer` | Fix the `systemd --user` line first. |
| `gateway port 8000` or `dash port 7000` | FAIL | Another program uses the port. Stop it, or change `listen` in `[gateway]` or `[dash]` of `kiln.toml` (for example `"127.0.0.1:8100"`). Then run `kiln check` and `systemctl --user restart kiln-gateway kiln-dash`. |
| `engine` | WARN, no active engine | Run `kiln switch crucible` (step 4). |
| `pack crucible` | FAIL | The profile does not point to a pack directory with `config.json`. Fix the pack path in `ExecStart` of the profile in `kiln.toml`. Or run step 2 again with the right `--pack` and `--force`. `--force` replaces all of `kiln.toml`. |
| `engine` | FAIL, `another process holds the port` | A server runs already, for example one that you started by hand. Stop it (step 0), then run `kiln switch crucible`. |
| `engine` | FAIL, does not answer | The server did not start or stopped. Read `journalctl --user -u kiln-crucible -n 50`, then run `kiln switch crucible` again. |
| `/health` shows `"state": "failed"` | | The engine card of the dashboard shows the reason and the last log line. Read `journalctl --user -u kiln-crucible -n 50`. Fix the cause, then run `kiln switch crucible` again. |
| Engine card: `port 8790 is in use by another process` | | A server runs already, for example one that you started by hand. Stop it (step 0), then run `kiln switch crucible` again. |
| `engine log` | WARN, no `crucible_*` lines | Send one request (step 5). If no lines appear, check that the profile runs the public Crucible server (`python -m server`), v0.33.1 or newer. |
| `kiln init`: `--crucible is empty` or `no server/__main__.py` | | `--crucible` must name the Crucible checkout. In a new terminal, set `CRUCIBLE` again (step 2). |
| `kiln init`: `build/engine/crucible: not found` | | Build the engine first (`make` in the checkout, Crucible setup guide step 4). |
| `kiln init`: `--pack is empty`, `not a directory` or `no config.json` | | `--pack` must name the directory of the pack, the one that holds `config.json`. In a new terminal, set `CKPT` again (step 2). |
| `kiln check`: `kiln: PATH: not valid TOML` or another problem | | Fix the line that the message names in `kiln.toml`. Then run `kiln check` again. |
| `kiln init`: `these files exist` | | Run the command again with `--force` to replace them. `--force` removes your edits in `kiln.toml` (step 2). |
| A client gets 403 `Origin '...' is not allowed here` | | A browser app calls the gateway. Add its origin (`scheme://host:port`) to `[gateway] allowed_origins` in `kiln.toml`, then restart `kiln-gateway`. |
| A client that uses a host name gets 403, and the same request to the IP address works | | Add the name to `[gateway] allowed_hosts` (and to `[dash] allowed_hosts` for the dashboard), then restart the two units. |
| A client gets 415 `the gateway does not accept a compressed request body` | | Set the client to send the body without compression. |
| `kiln switch` or `kiln stop`: `kiln-dash does not answer on 127.0.0.1:7000` | | The dashboard does not run. Run `systemctl --user status kiln-dash` and `journalctl --user -u kiln-dash -n 50`. Start it with `systemctl --user start kiln-dash`. |
| `kiln init`: `Started kiln-dash, but 127.0.0.1:7000 does not answer yet` | | Wait 10 s and run `kiln doctor`. If the port line is still not PASS, read `journalctl --user -u kiln-dash -n 50`. |
| `kiln: command not found` | | Run `uv tool update-shell` and open a new terminal. |
| The server stops at start with an out-of-memory message | | Close other programs that use memory. Use a smaller `--ctx` or `--slots` (step 2, with `--force`). Then run `kiln switch crucible`: the engine starts with the new command only after a switch. |

Please report a step that does not work for you as an issue, with the output of `kiln doctor`.
