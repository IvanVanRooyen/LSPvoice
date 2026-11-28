# Running on a server with Docker (staff-testing setup)

How to run the voice agent unattended on one Linux server (written for NixOS; any Linux with
Docker works) behind a **Cloudflare named tunnel**, so the Twilio number points at a fixed
address. This is a staff-testing setup, not production: see `deployment-plan.md` for that.

Examples use `voice.example.com` for the public hostname and `<server>` for the machine. Replace
both with the real names (kept out of this repository).

## What runs

`compose.yaml` starts three containers from one image (`Dockerfile`):

| Service | What it does | Reachable from |
|---|---|---|
| `agent` | The voice agent (`app.py`, one worker) | `127.0.0.1:8101` on the server, and the tunnel container |
| `cloudflared` | Optional (profile `tunnel`): Cloudflare named tunnel `https://voice.example.com` → `http://agent:8101` | Outbound connection to Cloudflare only |
| `dashboard` | The admin dashboard, with knowledge/flow/scenario editing **off** | `127.0.0.1:8102` on the server only, through SSH |

Data lives outside the image, in `./data` next to `compose.yaml` (or `LSPCARE_DATA`):

| Folder | Contents |
|---|---|
| `data/private/` | `twilio-pilot.json`, `zoho-desk.json`, `lspcare-inventory.json`, and everything written during calls: `transcripts/`, `reviews/`, `issues.json`, `feedback.jsonl`, `settings-log.jsonl` |
| `data/runs/` | Test-run results from the dashboard's Tests page |

The containers run as uid **10001** with a read-only filesystem. The agent refuses credentials
that anyone else can read, so `data/` must be owned by 10001 with folders 700 and files 600.

## 1. Server preparation (NixOS)

In `configuration.nix`:

```nix
virtualisation.docker.enable = true;
users.users.<your-user>.extraGroups = [ "docker" ];
environment.systemPackages = [ pkgs.git ];
```

`sudo nixos-rebuild switch`, then log out and in. Check `docker compose version`; if the compose
plugin is missing, add `pkgs.docker-compose` and use `docker-compose` instead of `docker compose`.

## 2. Code

```sh
git clone <repository URL> ~/lspvoice && cd ~/lspvoice
```

## 3. Cloudflare named tunnel

**If the server already runs `cloudflared`** for its own tunnel, add a public hostname to that
tunnel instead: `voice` . `example.com`, service **HTTP**, URL `127.0.0.1:8101` (no Access).
Leave `TUNNEL_TOKEN` empty in `.env` (`cp .env.example .env && chmod 600 .env`) and skip to step 4.

**Otherwise**, use the `cloudflared` container. In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared**.
Name it (e.g. `lspvoice`) and copy the **token** from the install command shown (the long value
after `--token`). Then add a **public hostname** (also called a published application route):

- Subdomain and domain: `voice` . `example.com`
- Service: **HTTP**, URL `agent:8101`

Do **not** put Cloudflare Access (sign-in) in front of this hostname: Twilio must reach it. WebSockets
are on by default and needed (`/relay`).

```sh
cp .env.example .env && chmod 600 .env
# edit .env: TUNNEL_TOKEN=<token>
```

## 4. Private data

Copy the private folder from the PC that ran the pilot (`...\Twilio AI\private-config\`) to the
server. At least `twilio-pilot.json`, `zoho-desk.json` and `lspcare-inventory.json`; copy
`transcripts/`, `reviews/`, `issues.json` and `feedback.jsonl` too to keep the call history.
Use `scp` (never email, chat or a shared drive). Then:

```sh
mkdir -p data/private data/runs
cp -r ~/private-config-incoming/. data/private/      # wherever scp put it
sudo chown -R 10001:10001 data
sudo find data -type d -exec chmod 700 {} +
sudo find data -type f -exec chmod 600 {} +
rm -rf ~/private-config-incoming
```

## 5. Build and start

```sh
export GIT_COMMIT=$(git rev-parse --short HEAD)
docker compose up -d --build                  # with the container tunnel: docker compose --profile tunnel up -d --build
docker compose ps            # agent and dashboard should become "healthy"
docker compose logs -f agent
```

`GIT_COMMIT` is written into every call transcript as the version and tags the image, so an
earlier version can be started again.

## 6. Point the agent and the Twilio number at the fixed address

The order matters: Twilio signs each request with the URL it called, and the agent checks the
signature against its saved public URL.

```sh
docker compose exec agent python configure.py --public-url https://voice.example.com
curl -s https://voice.example.com/health      # credentials_present true, inventory active
docker compose exec agent python configure.py --update-number
```

`--update-number` checks `/health` first, then points the number's Voice webhook at
`https://voice.example.com/voice` and reads it back. No restart is needed: settings are read
on every call.

**Stop `run.ps1` on the PC.** From this version on, a PC run will not take the number back from a
fixed address (it prints a yellow message instead); use `configure.py --update-number --force` on
the PC only to move the number back on purpose.

## 7. Check

1. Make a test call: greeting, a short exchange, the call reference at the end.
2. Open the dashboard: `ssh -L 8102:127.0.0.1:8102 <server>`, then http://localhost:8102.
   The call should be on the Calls and Live call pages with timing, tokens and cost.
3. Optional: run a few scenarios from the Tests page (about US$0.10).

## Operating

| Task | Command |
|---|---|
| Logs | `docker compose logs -f agent` (also `cloudflared`, `dashboard`) |
| Restart | `docker compose restart agent` |
| Change a setting | Dashboard → Settings, or `docker compose exec agent python configure.py --set tickets=dry_run` |
| Update to new code | `git pull && export GIT_COMMIT=$(git rev-parse --short HEAD) && docker compose up -d --build` |
| Roll back | `git checkout <commit>` then the update command (or `GIT_COMMIT=<commit> docker compose up -d` if that image is still on the server) |
| Stop | `docker compose down` (the number then fails until it points somewhere else) |
| Back to the PC | On the PC: `run.ps1`, then `configure.py --update-number --force` |

Knowledge, flow and scenario edits are off on the server dashboard: those files are part of the
image. Make them on a development PC, commit, then update the server.

Before leaving it unattended, set a monthly spend limit in the Anthropic Console, and decide
whether tickets should be `live` or `dry_run` (Settings).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Caller hears "an application error has occurred"; Twilio debugger shows 11200 | Tunnel down (`docker compose ps`, `logs cloudflared`), wrong route (`http://127.0.0.1:8101` for the server's own cloudflared, `http://agent:8101` for the container), or Cloudflare security challenging Twilio: check Security → Events for the hostname; turn off Bot Fight Mode or add a WAF skip rule for it |
| Agent answers but every call fails at once, log shows 403 | Saved public URL does not match the address Twilio calls: repeat step 6 |
| `agent` unhealthy or restarting | `docker compose logs agent`. "readable by others" or permission errors: repeat the `chown`/`chmod` in step 4 |
| Dashboard says "Local dashboard only." | Open exactly `http://localhost:8102` through the SSH tunnel |
| Times in transcripts are off | `TZ` in `.env` |
| Image build fails installing a package | A dependency has no ready-made wheel for this Python/CPU: add `build-essential` to the `apt-get install` line in the `Dockerfile` |
