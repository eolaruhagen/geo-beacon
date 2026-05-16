# OpenClaw/NemoClaw DGX setup log and migration runbook

Date: 2026-05-16

Purpose: preserve the full OpenClaw/NemoClaw setup state from the current DGX so the project can be moved to a fresh DGX with a local Nemotron model without relying on memory.

This is intentionally long. It records the working state, every major failure mode we hit, the fixes that worked, what is temporary inside the running container, and the clean migration path for the new machine.

No API keys, bearer tokens, passwords, or private values are included here. Commands that involve secrets use placeholders or interactive prompts.

## Current working result

The current DGX setup reached the goal:

- The DGX host runs the Geo-Beacon repository at `/home/asus/geo-beacon`.
- The host MCP server is running and reachable from the OpenClaw sandbox.
- The MCP server reads and writes the SAR SQLite/SpatiaLite database.
- OpenClaw is running inside the NemoClaw/OpenShell sandbox.
- OpenClaw can call the cloud NVIDIA/Nemotron model through the OpenShell managed inference route.
- OpenClaw sees the `geo-beacon-sar` MCP server and its tools.
- The full agent test succeeded: the model reasoned through the SAR prompt, called Geo-Beacon MCP tools, and wrote a dispatch to SQLite.

The successful OpenClaw agent tool path was:

1. `geo-beacon-sar__get_mission_brief`
2. `geo-beacon-sar__get_searcher`
3. `geo-beacon-sar__get_segment`
4. `geo-beacon-sar__dispatch_searcher`

The final agent response was:

```text
Dispatched ALPHA to segment S-r00-c01 for a hasty sweep. Action completed.
```

SQLite verification showed:

```text
dispatches:
1|ALPHA|S-r00-c01|hasty|pending

users:
1|ALPHA|dispatched|1

dispatch instruction:
Proceed to segment S-r00-c01 and conduct hasty sweep

dispatch reasoning:
ALPHA is standby with no active dispatch. Segment S-r00-c01 has highest remaining probability at 17% (POA=17%, POD=0%). Mission is active with subject last seen 185 minutes ago at PLS.
```

That confirms the model did not only answer in text. It actually called the MCP write tool, which wrote the dispatch to the SQLite database.

## Machines and paths

Current DGX SSH target:

```text
asus@gx10-d8fb
```

Current repo on the DGX:

```text
/home/asus/geo-beacon
```

Current local development repo in this Codex workspace:

```text
/home/gaurav/geo-beacon
```

NemoClaw sandbox name:

```text
my-assistant
```

Current Docker container:

```text
container id: 1cccb6818aa2
container name: openshell-my-assistant-87d0e768-1595-491a-aa25-7d09aa52a0d4
image: openshell/sandbox-from:1778935722
```

OpenClaw home inside the sandbox:

```text
/sandbox
```

OpenClaw config inside the sandbox:

```text
/sandbox/.openclaw/openclaw.json
```

OpenClaw workspace inside the sandbox:

```text
/sandbox/.openclaw/workspace
```

Current test database used for the successful LLM dispatch test:

```text
/home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db
```

Host MCP endpoint exposed to the sandbox:

```text
http://172.17.0.1:8765/mcp
```

OpenClaw gateway inside the sandbox:

```text
127.0.0.1:18790
```

OpenShell inference proxy inside the sandbox:

```text
http://10.200.0.1:3128
```

Docker network facts observed:

```text
sandbox container IP: 172.18.0.2
Docker gateway:       172.18.0.1
host.openshell.internal -> 172.18.0.1
host.docker.internal    -> 172.18.0.1
```

Important: `inference.local` does not resolve through normal DNS in the sandbox. That is expected in this setup. It is meant to be reached through the OpenShell HTTP/HTTPS proxy path, not through `/etc/hosts`.

## Repo files that matter for OpenClaw

These files are part of the Git repo and should transfer to the new DGX:

```text
agent/skills/read.py
agent/skills/write.py
agent/brief.py
agent/mcp_server.py
workers/agent.py
openclaw/SOUL.md
openclaw/TOOLS.md
openclaw/agent_prompt.md
openclaw/config.example.toml
scripts/run_agent_mcp_http.sh
scripts/install_openclaw_workspace.sh
.env.example
AGENTS.md
```

The key design is:

- `agent/skills/read.py` exposes read-side SAR functions.
- `agent/skills/write.py` exposes write-side SAR actions.
- `agent/brief.py` composes the mission brief that the model can ingest.
- `agent/mcp_server.py` exposes the Python functions as MCP tools.
- `scripts/run_agent_mcp_http.sh` starts the MCP server on the DGX host over streamable HTTP.
- `scripts/install_openclaw_workspace.sh` copies project context into the OpenClaw sandbox workspace and registers the MCP server.
- `openclaw/SOUL.md` and `openclaw/TOOLS.md` tell OpenClaw what it is and how to use the Geo-Beacon tools.

The MCP server now supports streamable HTTP through environment variables:

```text
GEO_BEACON_MCP_TRANSPORT=streamable-http
GEO_BEACON_MCP_HOST=172.17.0.1
GEO_BEACON_MCP_PORT=8765
```

The host MCP start script defaults to:

```text
host: 172.17.0.1
port: 8765
path: /mcp
```

The OpenClaw MCP registration inside the sandbox currently points to:

```json
{
  "url": "http://172.17.0.1:8765/mcp",
  "transport": "streamable-http"
}
```

## Current host MCP server state

The host MCP server was started in a tmux session on the DGX:

```text
tmux session: geo-beacon-mcp
process: python running the MCP HTTP server
log: /home/asus/geo-beacon/logs/mcp-http.log
```

The launch command used:

```bash
cd /home/asus/geo-beacon
MISSION_DB_PATH=/home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db ./scripts/run_agent_mcp_http.sh >> /home/asus/geo-beacon/logs/mcp-http.log 2>&1
```

The host MCP server was verified from inside the sandbox:

```bash
curl -sS --max-time 8 -H Accept:text/event-stream http://172.17.0.1:8765/mcp
```

The expected response for this low-level probe was not a successful MCP session. It was:

```json
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}
```

That response is good for a network probe. It proves the sandbox can reach the host MCP HTTP server. It is not a full MCP client handshake.

## Current database test fixture

The successful agent test used this SQLite database:

```text
/home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db
```

The mission was:

```text
mission: OpenClaw LLM Dispatch Test
mission_id: 1
user_id: 1
callsign: ALPHA
initial user status: standby
top segment: S-r00-c01
segment_id: 4
expected action: dispatch ALPHA to S-r00-c01 for hasty sweep
```

The tool wrote exactly one dispatch row:

```text
1|ALPHA|S-r00-c01|hasty|pending
```

And updated the user:

```text
1|ALPHA|dispatched|1
```

Useful verification commands:

```bash
sqlite3 /home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db \
  'select d.id,u.callsign,s.name,d.sweep_type,d.status from dispatches d join users u on u.id=d.user_id join segments s on s.id=d.segment_id order by d.id;'

sqlite3 /home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db \
  'select id,callsign,status,current_mission_id from users order by id;'

sqlite3 /home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db \
  'select id,instruction,reasoning from dispatches order by id;'
```

Notes:

- The test DB has no `events` table.
- The `users` table does not have `current_segment_id`.
- Searcher assignment is reflected by `users.status` plus the latest dispatch row.

## Model/provider setup on current DGX

Current inference path:

```text
OpenClaw inside sandbox
  -> OpenClaw model provider named inference
  -> https://inference.local/v1
  -> OpenShell inference route
  -> NVIDIA cloud provider nvidia-prod
  -> nvidia/nemotron-3-super-120b-a12b
```

The cloud provider was configured on the DGX host with a command shaped like this:

```bash
read -s -p "NVIDIA API key: " NVIDIA_API_KEY
echo
openshell -g nemoclaw provider delete nvidia-prod
openshell -g nemoclaw provider create --name nvidia-prod --type nvidia --credential NVIDIA_API_KEY="$NVIDIA_API_KEY"
unset NVIDIA_API_KEY
nemoclaw inference set --provider nvidia-prod --model nvidia/nemotron-3-super-120b-a12b --sandbox my-assistant
```

Observed output:

```text
Deleted provider nvidia-prod
Created provider nvidia-prod
Setting OpenShell inference route: nvidia-prod / nvidia/nemotron-3-super-120b-a12b
Gateway inference configured:
  Route: inference.local
  Provider: nvidia-prod
  Model: nvidia/nemotron-3-super-120b-a12b
  Version: 4 or 5
  Timeout: 60s
  Validated Endpoints:
    - https://integrate.api.nvidia.com/v1/chat/completions (openai_chat_completions)
Cannot read openclaw config (/sandbox/.openclaw/openclaw.json).
Is the sandbox running?
```

That message was confusing but important.

What it meant:

- The NVIDIA key/provider path was valid.
- The OpenShell inference route was configured.
- The host-side NemoClaw command could not read the sandbox OpenClaw config through OpenShell's sandbox registry/execution layer.
- It did not mean the API key was wrong.

Later verification proved the key and route were working:

```bash
openshell -g nemoclaw inference get
```

Showed:

```text
Gateway inference:
  Provider: nvidia-prod
  Model: nvidia/nemotron-3-super-120b-a12b
  Version: 5
  Timeout: 60s
```

Raw proxy chat completion from inside the sandbox also worked:

```bash
curl -sS --proxy http://10.200.0.1:3128 \
  -H Content-Type:application/json \
  -d @/tmp/inference-test.json \
  https://inference.local/v1/chat/completions
```

The response returned a real `chat.completion` object from:

```text
nvidia/nemotron-3-super-120b-a12b
```

## Official NVIDIA migration docs summary

NVIDIA's DGX Spark playbook used for reference:

```text
https://build.nvidia.com/spark/nemoclaw/instructions
```

The official playbook says the fresh DGX path is:

1. Configure Docker with the NVIDIA runtime.
2. Set Docker `default-cgroupns-mode` to `host`.
3. Restart Docker.
4. Install Ollama.
5. Configure Ollama with `OLLAMA_HOST=0.0.0.0` through systemd.
6. Pull `nemotron-3-super:120b`.
7. Install NemoClaw with:

```bash
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
```

8. During onboarding choose:

```text
Inference provider: Local Ollama
Model: nemotron-3-super:120b
Sandbox name: my-assistant or another lowercase-hyphen name
Policy presets: accept suggested presets
```

9. Connect to the sandbox:

```bash
nemoclaw my-assistant connect
```

10. Verify inference inside the sandbox:

```bash
curl -sf https://inference.local/v1/models
```

11. Test the agent:

```bash
openclaw agent --agent main -m "hello" --session-id test
```

Important official notes:

- The model download is large, about 87 GB.
- First local responses may take 30 to 90 seconds for the 120B model.
- Ollama should be started through systemd, not as a manual `ollama serve &`.
- If Docker gives permission errors, add the user to the Docker group and refresh the login session.
- The web UI should use `127.0.0.1` exactly when accessing the local forwarded UI because origin checks can be strict.

## Major issues encountered and what they meant

### 1. `nemoclaw inference set` validated NVIDIA but could not read OpenClaw config

Symptom:

```text
Cannot read openclaw config (/sandbox/.openclaw/openclaw.json).
Is the sandbox running?
```

Initial suspicion was that the config was owned by the wrong user. That was partly true, but not the whole story.

The real picture:

- `/sandbox/.openclaw/openclaw.json` existed.
- `HOME=/sandbox openclaw config validate` inside the container could validate the config.
- Host-side `nemoclaw inference set` reads config by calling OpenShell's sandbox exec layer, roughly:

```text
openshell sandbox exec --name my-assistant -- cat /sandbox/.openclaw/openclaw.json
```

- But `openshell sandbox list` and similar paths were failing with a protobuf decode error.

Observed OpenShell/NemoClaw error:

```text
failed to decode Protobuf message: Sandbox.id:
ListSandboxesResponse.sandboxes: invalid string value: data is not UTF-8 encoded
```

Effect:

- NemoClaw's normal host-side sandbox registry path was broken.
- Direct Docker access still worked.
- Therefore we used direct `docker exec` for the repair path.

### 2. Running `openclaw doctor --fix` as root in the container fixed the wrong home

The user entered the container as root:

```bash
docker exec -it 1cccb6818aa2 bash
```

Then ran:

```bash
openclaw doctor --fix
```

Because that shell was root, OpenClaw used:

```text
/root/.openclaw/openclaw.json
```

not:

```text
/sandbox/.openclaw/openclaw.json
```

That doctor run created/fixed root-owned OpenClaw state, which was not the actual sandbox user's OpenClaw runtime state.

Correct pattern for the real sandbox user:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc 'HOME=/sandbox openclaw config validate'
```

Or, for commands that must repair ownership:

```bash
docker exec -u root 1cccb6818aa2 chown 998:998 /sandbox/.openclaw/openclaw.json
```

Takeaway:

- Use `HOME=/sandbox`.
- Prefer `docker exec -u sandbox` for OpenClaw commands.
- Use `docker exec -u root` only for ownership/trust-store fixes.

### 3. `inference.local` did not resolve with normal DNS

Symptom:

```bash
docker exec 1cccb6818aa2 getent hosts inference.local
```

returned no rows.

And:

```bash
curl -k -I https://inference.local/v1/models
```

failed with:

```text
Could not resolve host: inference.local
```

This looked like DNS was broken. But NemoClaw's own startup comments say not to add `inference.local` to `NO_PROXY` or `/etc/hosts`, because it is intentionally routed through the OpenShell proxy path.

Working test:

```bash
docker exec 1cccb6818aa2 curl -sk --proxy http://10.200.0.1:3128 https://inference.local/v1/models
```

That returned the NVIDIA model list and included:

```text
nvidia/nemotron-3-super-120b-a12b
```

Takeaway:

- Direct DNS failure for `inference.local` is not the primary issue.
- The sandbox needs the OpenShell proxy environment active.
- The OpenClaw gateway must be launched with that proxy environment.

### 4. `/tmp/nemoclaw-proxy-env.sh` was missing

The key missing runtime file was:

```text
/tmp/nemoclaw-proxy-env.sh
```

Observed:

```text
PROXY_ENV_MISSING
```

NemoClaw normally writes this file during sandbox startup/connect. The source comments say it is the single source of truth for:

- `HTTP_PROXY`
- `HTTPS_PROXY`
- `NO_PROXY`
- lowercase proxy variants
- `NODE_OPTIONS` preload guards
- Nemotron request-shaping preload
- HTTP proxy fix preload
- sandbox safety preload
- ciao network guard preload
- seccomp guard preload

Because it was missing, our manually started OpenClaw gateway did not inherit the correct proxy path for `inference.local`.

### 5. OpenClaw gateway was initially started without proxy env

The first manual gateway command was shaped like:

```bash
HOME=/sandbox openclaw gateway run --bind loopback --port 18790 --ws-log compact
```

It started, but model calls failed:

```text
FailoverError: LLM request failed: network connection error.
```

Raw curl through the proxy worked, but OpenClaw did not because the OpenClaw process did not have:

```text
HTTP_PROXY=http://10.200.0.1:3128
HTTPS_PROXY=http://10.200.0.1:3128
NODE_USE_ENV_PROXY=1
NODE_OPTIONS=...NemoClaw preloads...
```

Takeaway:

- Starting the gateway manually without NemoClaw's env can make the gateway appear healthy while inference fails.
- The gateway must be started with the NemoClaw proxy env sourced.

### 6. TLS failed through the proxy until the OpenShell sandbox CA was trusted

With the proxy path, this worked only with insecure curl:

```bash
curl -sk --proxy http://10.200.0.1:3128 https://inference.local/v1/models
```

Without `-k`, curl failed:

```text
SSL certificate problem: self-signed certificate in certificate chain
```

The OpenShell proxy presents a certificate chain with:

```text
CN=inference.local
issuer=CN=OpenShell Sandbox CA, O=OpenShell
```

Fix used:

1. Inspect the certificate chain through the proxy:

```bash
docker exec 1cccb6818aa2 timeout 8 openssl s_client \
  -proxy 10.200.0.1:3128 \
  -connect inference.local:443 \
  -servername inference.local \
  -showcerts
```

2. Install the OpenShell Sandbox CA certificate into the sandbox trust store:

```bash
docker exec -u root 1cccb6818aa2 sh -lc '
  # Write the OpenShell Sandbox CA cert to:
  # /usr/local/share/ca-certificates/openshell-sandbox-ca.crt
  chmod 644 /usr/local/share/ca-certificates/openshell-sandbox-ca.crt
  update-ca-certificates
'
```

After that:

```bash
docker exec 1cccb6818aa2 curl -sS --proxy http://10.200.0.1:3128 \
  https://inference.local/v1/models -o /tmp/models-ok.json
```

worked without disabling TLS verification.

Important:

- Do not reuse the current DGX's CA certificate on the new DGX.
- Extract/trust the CA generated by the new OpenShell gateway, or let NemoClaw create the proper CA wiring during normal onboarding.

### 7. Do not set `NODE_TLS_REJECT_UNAUTHORIZED=0` permanently

An attempted shortcut was to put this in the proxy env:

```bash
export NODE_TLS_REJECT_UNAUTHORIZED=0
```

That was rejected because it disables TLS verification for Node processes.

Correct fix:

- Install the OpenShell Sandbox CA into the container trust store.
- Set:

```bash
export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"
export CURL_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
export NODE_EXTRA_CA_CERTS="/usr/local/share/ca-certificates/openshell-sandbox-ca.crt"
```

### 8. Stale gateway process held a session lock

After the proxy env was reconstructed, a local OpenClaw inference attempt no longer failed with network error. It failed with:

```text
SessionWriteLockTimeoutError:
session file locked (timeout 10000ms):
pid=870 /sandbox/.openclaw/agents/main/sessions/...jsonl.lock
```

That meant the old gateway process was still alive and holding a session lock.

Fix:

```bash
docker exec -u root 1cccb6818aa2 sh -lc '
  pkill -TERM -f openclaw-gateway 2>/dev/null || true
  sleep 2
  pkill -KILL -f openclaw-gateway 2>/dev/null || true
'
```

Then restart the gateway with the repaired env.

### 9. Redirecting gateway logs to a root-owned file prevented sandbox-user startup

We tried:

```bash
docker exec -u sandbox -d 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  openclaw gateway run --bind loopback --port 18790 --ws-log compact >> /sandbox/.openclaw/logs/manual-gateway.log 2>&1
'
```

But `/sandbox/.openclaw/logs/manual-gateway.log` was root-owned from earlier commands:

```text
-rw-r--r-- 1 0 998 ... /sandbox/.openclaw/logs/manual-gateway.log
```

The sandbox user could not append to it, so the shell exited before OpenClaw started.

Fix:

- Do not redirect to the root-owned manual log file.
- Let OpenClaw write its normal sandbox-user log:

```text
/tmp/openclaw-998/openclaw-2026-05-16.log
```

Working detached gateway command:

```bash
docker exec -u sandbox -d 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  exec openclaw gateway run --bind loopback --port 18790 --ws-log compact
'
```

### 10. OpenClaw device auth needed another scope approval

After restarting the gateway as the sandbox user, CLI calls hit:

```text
scope upgrade pending approval
```

`openclaw devices list` showed a pending request:

```text
request id: 00d16a4c-8bea-4764-b6c4-6fc0b0dc3866
requested scopes:
  operator.admin
  operator.approvals
  operator.pairing
  operator.read
  operator.talk.secrets
  operator.write
```

Fix:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  openclaw devices approve REQUEST_ID --json
'
```

The user explicitly approved this class of OpenClaw operator scope upgrade earlier.

### 11. OpenClaw identity files were root-owned

Gateway status showed:

```text
EACCES: permission denied, open '/sandbox/.openclaw/identity/device-auth.json'
```

Ownership before fix:

```text
-rw------- 1 0 998 /sandbox/.openclaw/identity/device-auth.json
```

Fix:

```bash
docker exec -u root 1cccb6818aa2 chown -R 998:998 /sandbox/.openclaw/identity
```

### 12. Plugin runtime deps were root-owned

The full `openclaw agent` CLI initially failed with many errors like:

```text
PluginLoadFailureError:
failed to install bundled runtime deps:
Error: EACCES: permission denied, unlink '/sandbox/.openclaw/plugin-runtime-deps/...'
```

Cause:

- Earlier root OpenClaw commands created or modified plugin runtime files.
- The sandbox user then could not update/unlink them.

Fix:

```bash
docker exec -u root 1cccb6818aa2 chown -R 998:998 /sandbox/.openclaw/plugin-runtime-deps
```

After this, `openclaw agent` succeeded.

### 13. `openclaw doctor --fix` hit npm cache-only dependency errors

Running doctor produced many messages like:

```text
npm error code ENOTCACHED
npm error request to https://registry.npmjs.org/... failed:
cache mode is 'only-if-cached' but no cached response is available.
```

What it meant:

- The sandbox was trying to stage bundled plugin runtime dependencies.
- The npm mode/cache state did not allow fetching missing packages.
- Many optional/bundled plugins failed to initialize.

Effect on SAR MCP test:

- Not fatal for the core model/MCP test.
- The gateway could still run.
- The Geo-Beacon MCP tools loaded and were available.
- The agent successfully called the SAR tools.

Still, on the new DGX, this should be avoided by using a clean NemoClaw onboard flow rather than running OpenClaw as root inside the sandbox.

### 14. Stale `openclaw-weixin` plugin warning

Repeated warnings:

```text
plugins: blocked plugin candidate: suspicious ownership (/sandbox/.openclaw/extensions/openclaw-weixin, uid=998, expected uid=0 or root)
plugins.entries.openclaw-weixin: plugin not found: openclaw-weixin
```

Effect:

- Annoying but not blocking.
- The SAR MCP server still registered and worked.

Future cleanup:

- Remove the stale plugin entry from OpenClaw config if it continues to pollute startup logs.
- Do not prioritize this unless it blocks agent startup on the new DGX.

## Manual repair state currently inside the container

These changes are inside the running Docker container and may not survive sandbox rebuilds:

1. Ownership fixes:

```bash
chown 998:998 /sandbox/.openclaw/openclaw.json
chown -R 998:998 /sandbox/.openclaw/identity
chown -R 998:998 /sandbox/.openclaw/plugin-runtime-deps
```

2. OpenShell Sandbox CA installed into:

```text
/usr/local/share/ca-certificates/openshell-sandbox-ca.crt
```

and trust store updated with:

```bash
update-ca-certificates
```

3. NemoClaw preload guard scripts copied into `/tmp`:

```text
/tmp/nemoclaw-sandbox-safety-net.js
/tmp/nemoclaw-http-proxy-fix.js
/tmp/nemoclaw-nemotron-inference-fix.js
/tmp/nemoclaw-ciao-network-guard.js
/tmp/nemoclaw-seccomp-guard.js
```

4. Reconstructed runtime env file:

```text
/tmp/nemoclaw-proxy-env.sh
```

5. Gateway started as `sandbox` user:

```bash
docker exec -u sandbox -d 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  exec openclaw gateway run --bind loopback --port 18790 --ws-log compact
'
```

Because these are container-local, the new DGX should either:

- avoid needing them by doing clean NemoClaw onboarding, or
- turn them into a repeatable script if the same missing runtime env bug appears.

## Reconstructed `/tmp/nemoclaw-proxy-env.sh`

The current working env file is conceptually:

```bash
# Reconstructed NemoClaw proxy/runtime env for this running sandbox.
export HTTP_PROXY="http://10.200.0.1:3128"
export HTTPS_PROXY="http://10.200.0.1:3128"
export NO_PROXY="localhost,127.0.0.1,::1,10.200.0.1,172.17.0.1,172.18.0.1,host.openshell.internal,host.docker.internal"
export http_proxy="http://10.200.0.1:3128"
export https_proxy="http://10.200.0.1:3128"
export no_proxy="localhost,127.0.0.1,::1,10.200.0.1,172.17.0.1,172.18.0.1,host.openshell.internal,host.docker.internal"
export SSL_CERT_FILE="/etc/ssl/certs/ca-certificates.crt"
export CURL_CA_BUNDLE="/etc/ssl/certs/ca-certificates.crt"
export NODE_EXTRA_CA_CERTS="/usr/local/share/ca-certificates/openshell-sandbox-ca.crt"
export NODE_USE_ENV_PROXY=1
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require /tmp/nemoclaw-sandbox-safety-net.js"
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require /tmp/nemoclaw-http-proxy-fix.js"
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require /tmp/nemoclaw-nemotron-inference-fix.js"
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require /tmp/nemoclaw-seccomp-guard.js"
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--require /tmp/nemoclaw-ciao-network-guard.js"
```

The key details:

- `inference.local` must not be in `NO_PROXY`.
- `172.17.0.1` must be in `NO_PROXY` so the sandbox reaches the host MCP server directly.
- `10.200.0.1` must be in `NO_PROXY` to avoid proxy loops.
- `NODE_EXTRA_CA_CERTS` points to the OpenShell Sandbox CA.
- `NODE_USE_ENV_PROXY=1` lets Node use the proxy env.
- The NemoClaw preloads patch/protect Node behavior for sandbox networking and Nemotron request shape.

## Successful smoke tests

### Direct MCP reachability from sandbox

Command shape:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  curl -sS --max-time 8 -H Accept:text/event-stream http://172.17.0.1:8765/mcp
'
```

Expected low-level response:

```json
{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,"message":"Bad Request: Missing session ID"}}
```

### OpenClaw MCP list

Command:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  openclaw mcp list
'
```

Observed:

```text
MCP servers (/sandbox/.openclaw/openclaw.json):
- geo-beacon-sar
```

### OpenClaw model inference

Command:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  openclaw infer model run --gateway --json \
    --model inference/nvidia/nemotron-3-super-120b-a12b \
    --prompt ok
'
```

Observed:

```json
{
  "ok": true,
  "capability": "model.run",
  "transport": "gateway",
  "provider": "inference",
  "model": "nvidia/nemotron-3-super-120b-a12b",
  "outputs": [
    {
      "text": "I'm here and ready to assist with the Geo-Beacon SAR mission control. ..."
    }
  ]
}
```

### Full OpenClaw agent dispatch test

Prompt used:

```text
You are running the Geo-Beacon SAR dispatch smoke test.

Use the geo-beacon-sar MCP tools. First inspect the mission state/brief if a tool is available. Then perform the obvious action: ALPHA is standing by, and the highest-priority uncovered segment is S-r00-c01. Dispatch ALPHA to S-r00-c01 for a hasty sweep.

After taking the action, briefly report the concrete action you took. Do not ask for confirmation.
```

Observed output:

```json
{
  "status": "ok",
  "summary": "completed",
  "result": {
    "payloads": [
      {
        "text": "Dispatched ALPHA to segment S-r00-c01 for a hasty sweep. Action completed."
      }
    ],
    "toolSummary": {
      "calls": 4,
      "tools": [
        "geo-beacon-sar__get_mission_brief",
        "geo-beacon-sar__get_searcher",
        "geo-beacon-sar__get_segment",
        "geo-beacon-sar__dispatch_searcher"
      ],
      "failures": 0
    },
    "executionTrace": {
      "winnerProvider": "inference",
      "winnerModel": "nvidia/nemotron-3-super-120b-a12b",
      "fallbackUsed": false
    }
  }
}
```

Then SQLite showed the dispatch row.

## Recommended migration path to the new DGX

This is the clean path for a fresh DGX where the local model is preinstalled or can be pulled.

### Phase 0: before touching the new DGX

Make sure the current repo has all code committed and pushed:

```bash
git status
git add .
git commit -m "document openclaw dgx setup"
git push
```

Do not commit:

- NVIDIA API keys
- OpenClaw bearer tokens
- Telegram tokens
- `.db` files unless intentionally using a seed fixture
- generated OpenClaw credential stores
- `.env` with real secrets

### Phase 1: prepare Docker and GPU runtime on the new DGX

From the official NVIDIA playbook:

```bash
sudo nvidia-ctk runtime configure --runtime=docker
```

Set Docker cgroup namespace mode:

```bash
sudo python3 -c "
import json, os
path = '/etc/docker/daemon.json'
d = json.load(open(path)) if os.path.exists(path) else {}
d['default-cgroupns-mode'] = 'host'
json.dump(d, open(path, 'w'), indent=2)
"
```

Restart Docker:

```bash
sudo systemctl restart docker
```

Verify GPU runtime:

```bash
docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi
```

If Docker permissions fail:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

### Phase 2: install/configure Ollama on the new DGX

Install:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Configure Ollama to listen on all interfaces:

```bash
sudo mkdir -p /etc/systemd/system/ollama.service.d
printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0"\n' | sudo tee /etc/systemd/system/ollama.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Verify:

```bash
curl http://0.0.0.0:11434
```

Expected:

```text
Ollama is running
```

Pull or verify the model:

```bash
ollama pull nemotron-3-super:120b
ollama list
```

Warm it once:

```bash
ollama run nemotron-3-super:120b
```

Then type:

```text
/bye
```

Important:

- Do not use `ollama serve &` as the real setup.
- Use systemd so `OLLAMA_HOST=0.0.0.0` is active.
- The sandbox needs to reach Ollama from a container network path.

### Phase 3: install NemoClaw and create the sandbox

Install NemoClaw:

```bash
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
```

If `nemoclaw` is not found:

```bash
source ~/.bashrc
```

During onboarding:

```text
sandbox name: my-assistant
inference provider: Local Ollama
model: nemotron-3-super:120b
messaging: skip unless demo requires Telegram
policy presets: accept suggested presets
```

Connect:

```bash
nemoclaw my-assistant connect
```

Inside the sandbox, verify:

```bash
curl -sf https://inference.local/v1/models
```

Expected:

```text
JSON model list including nemotron-3-super:120b
```

Then:

```bash
openclaw agent --agent main -m "hello" --session-id test
```

### Phase 4: clone Geo-Beacon on the new DGX host

On the DGX host, not inside the sandbox:

```bash
cd /home/asus
git clone <GITHUB_REPO_URL> geo-beacon
cd /home/asus/geo-beacon
```

Set up Python:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

If SpatiaLite is available from apt:

```bash
sudo apt-get update
sudo apt-get install -y libsqlite3-mod-spatialite spatialite-bin
```

If apt/sudo is not available, reproduce the user-space SpatiaLite fallback from current DGX or document it as a setup script. Current DGX fallback path was:

```text
/home/asus/geo-beacon/dev/data/spatialite_pkg/root/usr/lib/aarch64-linux-gnu/mod_spatialite.so
```

with library dir:

```text
/home/asus/geo-beacon/dev/data/spatialite_pkg/root/usr/lib/aarch64-linux-gnu
```

### Phase 5: create or migrate the SQLite DB

Production path from project notes:

```text
/data/mission.db
```

For hackathon testing, it is okay to use a repo-local dev DB:

```text
/home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db
```

All Python processes are designed to apply migrations at startup through:

```text
scripts/apply_migrations.py
```

So the normal new-DGX flow is:

1. Put the DB at the target path.
2. Start FastAPI/workers/MCP.
3. Let startup apply migrations.

SQLite must be in WAL mode because phones, FastAPI, workers, and MCP tools can write concurrently.

Verify WAL mode:

```bash
sqlite3 /path/to/mission.db 'pragma journal_mode;'
```

Expected:

```text
wal
```

### Phase 6: start host MCP server on the new DGX

From the DGX host:

```bash
cd /home/asus/geo-beacon
mkdir -p logs
MISSION_DB_PATH=/path/to/mission.db ./scripts/run_agent_mcp_http.sh >> logs/mcp-http.log 2>&1
```

For a persistent demo session, run under tmux:

```bash
tmux new -s geo-beacon-mcp
cd /home/asus/geo-beacon
MISSION_DB_PATH=/path/to/mission.db ./scripts/run_agent_mcp_http.sh >> logs/mcp-http.log 2>&1
```

Expected listener:

```text
172.17.0.1:8765
```

Host-side quick check:

```bash
curl -i http://172.17.0.1:8765/mcp
```

Sandbox-side network check:

```bash
curl -sS --max-time 8 -H Accept:text/event-stream http://172.17.0.1:8765/mcp
```

Expected low-level response:

```text
Missing session ID
```

### Phase 7: install OpenClaw workspace files and MCP config

From the DGX host repo:

```bash
cd /home/asus/geo-beacon
./scripts/install_openclaw_workspace.sh
```

That script should:

- copy `openclaw/SOUL.md` into `/sandbox/.openclaw/workspace/SOUL.md`
- copy `openclaw/TOOLS.md` into `/sandbox/.openclaw/workspace/TOOLS.md`
- copy `AGENTS.md` into `/sandbox/.openclaw/workspace/AGENTS.md`
- register MCP server `geo-beacon-sar`

Manual equivalent inside the sandbox:

```bash
HOME=/sandbox openclaw mcp set geo-beacon-sar \
  '{"url":"http://172.17.0.1:8765/mcp","transport":"streamable-http"}'
```

Verify:

```bash
HOME=/sandbox openclaw mcp list
```

Expected:

```text
- geo-beacon-sar
```

### Phase 8: run new-DGX smoke tests

Inside the sandbox:

```bash
curl -sf https://inference.local/v1/models
```

Then:

```bash
HOME=/sandbox openclaw infer model run --gateway --json \
  --model inference/nemotron-3-super:120b \
  --prompt ok
```

The exact model ref may differ depending on how NemoClaw writes the local Ollama provider into OpenClaw config. If that model ref fails, inspect the configured model list with non-secret OpenClaw commands and use whatever `inference/...` model name NemoClaw created.

Then:

```bash
HOME=/sandbox openclaw mcp list
```

Then run the dispatch smoke prompt:

```text
You are running the Geo-Beacon SAR dispatch smoke test.

Use the geo-beacon-sar MCP tools. First inspect the mission state/brief if a tool is available. Then perform the obvious action: ALPHA is standing by, and the highest-priority uncovered segment is S-r00-c01. Dispatch ALPHA to S-r00-c01 for a hasty sweep.

After taking the action, briefly report the concrete action you took. Do not ask for confirmation.
```

Expected:

- Agent calls Geo-Beacon MCP read tools.
- Agent calls `dispatch_searcher`.
- SQLite gets a new dispatch row.
- The final answer reports the action.

## Current cloud setup versus new local setup

Current DGX:

```text
provider on OpenShell: nvidia-prod
provider type: NVIDIA cloud endpoint
model: nvidia/nemotron-3-super-120b-a12b
route exposed to OpenClaw: https://inference.local/v1
OpenClaw model ref: inference/nvidia/nemotron-3-super-120b-a12b
```

New DGX target:

```text
provider on OpenShell/NemoClaw: local Ollama
model in Ollama: nemotron-3-super:120b
route exposed to OpenClaw: https://inference.local/v1
OpenClaw model ref: likely inference/nemotron-3-super:120b or similar
```

The OpenClaw side should still call `https://inference.local/v1`. The provider behind that route changes from cloud NVIDIA API to local Ollama.

The MCP side should not change:

```text
geo-beacon-sar -> http://172.17.0.1:8765/mcp
```

The database side should not change:

```text
SQLite/SpatiaLite on the DGX host
Python MCP server on the DGX host
OpenClaw in sandbox calls host MCP over HTTP
```

## Things to automate next

The current manual work should become one or two scripts before the hardware move if there is time.

Suggested script 1:

```text
scripts/dgx_setup_geo_beacon.sh
```

Responsibilities:

- create Python venv
- install requirements
- verify SQLite and SpatiaLite
- initialize dev/test DB if needed
- apply migrations
- start MCP server under tmux
- print MCP endpoint

Suggested script 2:

```text
scripts/openclaw_sandbox_install.sh
```

Responsibilities:

- run `scripts/install_openclaw_workspace.sh`
- verify `HOME=/sandbox openclaw mcp list`
- verify sandbox can reach host MCP
- run an OpenClaw inference smoke test
- optionally run the dispatch smoke prompt

Suggested emergency repair script, only if new DGX hits the same container problems:

```text
scripts/repair_openclaw_sandbox_runtime.sh
```

Responsibilities:

- find the sandbox container by name
- copy NemoClaw preload scripts from `~/.nemoclaw/source/nemoclaw-blueprint/scripts/`
- extract/install OpenShell Sandbox CA dynamically
- write `/tmp/nemoclaw-proxy-env.sh`
- chown `/sandbox/.openclaw` subpaths to UID/GID 998
- restart OpenClaw gateway as sandbox user

This script should not include old machine-specific certs or secrets.

## Do not repeat these mistakes

1. Do not assume `inference.local` should resolve through `getent hosts`.
   It is proxy-routed.

2. Do not put `inference.local` in `NO_PROXY`.
   That forces direct DNS and breaks the intended route.

3. Do not run `openclaw doctor --fix` as root unless you intend to fix root's OpenClaw home.
   For the actual sandbox, use `HOME=/sandbox` and the `sandbox` user.

4. Do not permanently disable TLS with `NODE_TLS_REJECT_UNAUTHORIZED=0`.
   Install the OpenShell Sandbox CA instead.

5. Do not redirect the sandbox-user gateway into root-owned log files.
   Let OpenClaw write its own logs or fix log ownership first.

6. Do not expect host-side `nemoclaw inference set --sandbox my-assistant` to work if OpenShell sandbox registry is corrupted.
   Direct Docker can still be used to inspect/fix the container.

7. Do not forget `172.17.0.1` in `NO_PROXY`.
   The host MCP server must be reached directly, not through the OpenShell proxy.

8. Do not store the NVIDIA API key in the repo.
   Use interactive `read -s` and OpenShell provider credentials.

## Useful commands from current DGX

Find the sandbox container:

```bash
docker ps --format '{{.ID}} {{.Names}} {{.Status}}'
```

Expected current row:

```text
1cccb6818aa2 openshell-my-assistant-87d0e768-1595-491a-aa25-7d09aa52a0d4 Up ...
```

Validate OpenClaw config as sandbox user:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc 'HOME=/sandbox openclaw config validate'
```

Check host MCP from sandbox:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  curl -sS --max-time 8 -H Accept:text/event-stream http://172.17.0.1:8765/mcp
'
```

Check model route through proxy:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  curl -sS --proxy http://10.200.0.1:3128 --max-time 12 https://inference.local/v1/models -o /tmp/models-ok.json
'
```

Start gateway:

```bash
docker exec -u sandbox -d 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  exec openclaw gateway run --bind loopback --port 18790 --ws-log compact
'
```

Check gateway processes:

```bash
docker exec 1cccb6818aa2 sh -lc 'ps -ef | grep openclaw | grep -v grep || true'
```

Approve OpenClaw CLI scope request:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  openclaw devices list
  openclaw devices approve REQUEST_ID --json
'
```

Run inference smoke:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  openclaw infer model run --gateway --json \
    --model inference/nvidia/nemotron-3-super-120b-a12b \
    --prompt ok
'
```

Run MCP list:

```bash
docker exec -u sandbox 1cccb6818aa2 sh -lc '
  . /tmp/nemoclaw-proxy-env.sh
  export HOME=/sandbox
  openclaw mcp list
'
```

Verify dispatch:

```bash
sqlite3 /home/asus/geo-beacon/dev/data/openclaw_llm_dispatch_test.db \
  'select d.id,u.callsign,s.name,d.sweep_type,d.status from dispatches d join users u on u.id=d.user_id join segments s on s.id=d.segment_id order by d.id;'
```

## Minimum definition of done on the new DGX

The migration is successful only when all of these pass on the new hardware:

1. `ollama list` shows `nemotron-3-super:120b`.
2. `nemoclaw my-assistant connect` works.
3. Inside sandbox, `curl -sf https://inference.local/v1/models` returns the local model list.
4. `openclaw agent --agent main -m "hello" --session-id test` responds using the local model.
5. `/home/asus/geo-beacon` exists and is from GitHub.
6. Python dependencies install.
7. SQLite DB opens and uses WAL mode.
8. Host MCP server listens on `172.17.0.1:8765`.
9. Sandbox can reach `http://172.17.0.1:8765/mcp`.
10. `HOME=/sandbox openclaw mcp list` shows `geo-beacon-sar`.
11. OpenClaw agent sees tools named `geo-beacon-sar__...`.
12. Dispatch smoke test writes a row to SQLite.

## Final current state snapshot

At the end of this setup session:

- Git repo was clean before this document was added.
- Current DGX repo was pulled to the latest pushed commit before the OpenClaw work.
- Host MCP server was running and reachable.
- OpenClaw gateway was running as `sandbox`.
- OpenClaw inference through `inference/nvidia/nemotron-3-super-120b-a12b` worked.
- `geo-beacon-sar` MCP server was registered.
- Full agent dispatch smoke test succeeded.
- SQLite recorded the dispatch.
- The fixes are partly manual and container-local.

Most important migration lesson:

The repo code is ready to transfer through Git. The fragile part is not the SAR code. The fragile part is making sure NemoClaw/OpenShell creates a healthy sandbox runtime where:

- `inference.local` works through the proxy,
- OpenClaw runs as the `sandbox` user with `HOME=/sandbox`,
- the sandbox can directly reach the DGX host MCP server,
- ownership stays under UID/GID `998`,
- and the OpenClaw gateway is started with the NemoClaw proxy/preload environment.

