# Holo Resources — Discovery + Cross-Host Fan-Out

**Status:** design proposal · v0.1 · 2026-06-14 · _Phase 0 lock-in for the holo resources feature and tai's `on` keyword. No code yet — this doc captures the six architectural decisions that Phase 1+ implement against._

## Problem

Holo daemons today announce **sessions**: a single MCP endpoint per host, optionally with hardware/applications capabilities published over an HTTP endpoint. There is no concept of a per-host **resource** — a path, a mounted volume, a folder of files — that a holo-aware tool can discover and act on.

The driving use case: six macOS machines on the same LAN, two of them with USB drives mounted in `/Volumes`, all six holding mp4 video collections with cross-machine duplicates. The goal is for a tai script to discover every host announcing a `video-files` resource, fingerprint every mp4 below the announced path, and report duplicates — without enumerating hosts manually and without standing up a central index.

The feature spans both repos:

- **holo** ships the announce/discover/exec primitives.
- **tai** adds a new keyword `on` that consumes them.

This doc fixes the six load-bearing design decisions before either side starts coding.

## Goals

- A holo daemon can announce one or more **resources** (path + tags + capabilities) alongside its session.
- Any peer authorized by the holo cert chain can discover those resources by tag over mDNS, with cheap filtering.
- An authorized peer can execute a small shell body in the scope of a specific resource — with the body's command surface allowlisted at announce time and its working directory pinned to the resource path.
- A tai script uses one new keyword (`on`) to fan a shell body out to every host matching a selector, collect results, handle failures, and surface a per-host exit map.

## Non-goals (v1)

- No remote UI automation through the exec primitive. Holo's `screen_*`, `browser_*`, and `ui_template_*` builtins remain local-only. If a future version wants remote UI control, it surfaces as a distinct MCP tool, opt-in per call — never folded into the exec body.
- No tai stdlib of common fan-out patterns. Wait until 5+ user-written scripts exist before extracting helpers.
- No write-side helpers on the holo side (`holo_trash`, `holo_link`, `holo_move`). Current scripts compose `exec:mv`, `exec:rm`, etc.; revisit if patterns repeat.
- No central index, no backend coordination beyond cert issuance. Discovery stays mDNS-local; exec stays daemon-to-daemon over the auto-tunnel.

## Architecture overview

```
┌─ controller (tai script) ──────────────────────────────────────┐
│                                                                 │
│   on [holo:tag=video-files:5m] '…body…' /done else { … }       │
│                                                                 │
│        │                                                        │
│        ▼                                                        │
│   ┌─ tai runtime ──────────────────┐                            │
│   │ • zeroconf subscription (lazy) │                            │
│   │ • selector → host list         │                            │
│   │ • per-host MCP channel reuse   │                            │
│   └────────────┬───────────────────┘                            │
└────────────────┼────────────────────────────────────────────────┘
                 │ MCP over auto-tunnel (cert-authed)
                 ▼
┌─ daemon on each matching host ─────────────────────────────────┐
│                                                                 │
│   holo_exec_in_resource(resource, body, env, timeout)           │
│      │                                                          │
│      ▼                                                          │
│   ┌─ enforce ─────────────────────────────────────┐             │
│   │ • cert chain valid + fresh                    │             │
│   │ • principal allowed in resources.toml (v2)    │             │
│   │ • body parse: only `caps=exec:…` heads        │             │
│   │ • body parse: no abs paths / `..` in args     │             │
│   └─────────────┬─────────────────────────────────┘             │
│                 ▼                                               │
│   spawn `/bin/sh -c "$body"` with                               │
│     • cwd = resource.path                                       │
│     • PATH = symlink farm of allowed binaries                   │
│     • env += HOLO_HOST, HOLO_RESOURCE, HOLO_RESOURCE_PATH       │
│     • optional uid drop (L2) or sandbox-exec (L3, v2)           │
│                                                                 │
│   stream stdout/stderr/exit back over MCP                       │
└─────────────────────────────────────────────────────────────────┘
```

The six numbered sections below correspond to the six questions ratified during Phase 0 review.

---

## Q1 — Wire transport

**Decision: new MCP tool `holo_exec_in_resource` over the existing auto-tunnel + MCP channel; body runs via per-call `/bin/sh -c` (no tai-shell, no holo builtins on the remote).**

### Tool surface

```
holo_exec_in_resource(
  resource: str,           # resource name as declared in announce
  body: str,               # shell body, ≤ 64 KiB
  env: dict[str, str] = {},  # extra env vars, merged with daemon-injected ones
  timeout_seconds: int = 60,
) -> stream of:
  { "fd": "stdout" | "stderr", "data": str }   # interleaved as produced
  { "exit": int, "duration_ms": int }          # terminal frame
```

The daemon line-buffers both fd streams and emits one frame per line. The terminal `exit` frame closes the stream.

### Why this shape

- Auto-tunnel + MCP already carries the cert handshake, tunnel acquisition, keepalive. Don't rebuild any of that for a sibling endpoint.
- A dedicated tool method gives a single place to enforce auth, allowlist, and scope — all inside the daemon, before `sh` spawns.
- Per-call `/bin/sh -c` is the cleanest blast radius: timeout = SIGKILL the PID, no shared state, no remote tai-shell to attack. Reusing the long-running `tcsh` would mean every body could call holo builtins (`screen_click`, etc.) on the remote — a remote-UI-control capability whether the script asks for it or not. Excluded.

### Channel reuse

The tai runtime opens **one MCP channel per daemon per script** and reuses it for the script's lifetime. The auto-tunnel is expensive to acquire (cert refresh, SSH handshake, tunnel allocation); reopening per call wastes that cost. Channel closes on shell exit via an atexit handler in tai's embedded Python.

### What this commits us to

- MCP is the cross-host transport for resource work, not just session control. If MCP gets deprecated in the holo ecosystem, this surface migrates with it.
- Output framing is the daemon's job — not SSH's, not the transport's. The tool method defines the `{"fd": …, "data": …}` envelope; everything downstream (tai's mode flags in Q4) is a presentation layer over that envelope.

---

## Q2 — Auth + capabilities allowlist

**Decision: cert chain (existing) for transport identity + per-resource ACL on the daemon + binary-name allowlist enforced by static parse and PATH pinning.**

### Transport identity

Reuse the existing holo cert chain. The daemon validates the peer's chain against the configured backend (`HOLO_BACKEND`, default `api-dev.tai.sh`) at MCP-channel open. For each `holo_exec_in_resource` call, additionally check cert freshness — mtime within N minutes (default 60). Stale → reject with `cert-stale`; controller transparently runs `holo cert refresh` and retries. No parallel token-auth path.

### Per-resource ACL

> **v1 status: declarative only, not enforced.**
>
> Implementation discovered that holo's cert architecture provides a single fixed principal (`lando`, from `tunnel.py`) at the SSH/CloudCity layer and **no per-call identity** at the MCP layer where `holo_exec_in_resource` runs. `allow_principals` is parsed, surfaced through `/v1/resources` and `holo_list_resources`, and logged informationally — but **the cert chain on the MCP channel is the only access gate that actually fires in v1.** Multi-principal use needs either (a) backend changes so signed certs carry per-user principals + CloudCity sshd `AuthorizedPrincipalsCommand`, or (b) SSH→MCP plumbing so the MCP layer sees `SSH_USER_AUTH`-style info per call. Both are multi-week projects deferred to a later phase. The intent is locked in here so v1 daemons can already declare the ACL they want and operators can pre-author config; when enforcement lands it reads the same field.

The daemon's resource config lives at `~/.config/holo/resources.toml` (TOML, stdlib `tomllib` — chosen over YAML to avoid adding PyYAML as a dependency; `~/.config/holo/` is where `holo cert` already keeps state):

```toml
# ~/.config/holo/resources.toml

[resources.movies]
path = "/Volumes/movies"
tags = ["video-files", "archive"]
caps = ["exec:ffprobe", "exec:python3", "exec:find", "exec:awk", "readonly"]
allow_principals = ["alice@laptop", "*@home-lan"]   # parsed, not enforced (v1)

[resources.private-photos]
path = "/Users/me/Photos"
tags = ["photos"]
caps = ["exec:ffprobe"]
allow_principals = ["alice@laptop"]                 # parsed, not enforced (v1)
```

`--announce-resource 'name=…;path=…;tags=…;caps=…;allow_principals=…'` on the CLI is the smoke-test path; the TOML file (via `--resources-config PATH`) is for any daemon running for more than 10 minutes. The two are **mutually exclusive** — declare resources in one place. Principal grammar (`alice@laptop`, `*@home-lan`) is illustrative; the realistic enforcement layer needs decisions about cert subjects (see Open Implementation Details #1).

### Binary-name allowlist

`caps=exec:NAME` declares which binaries the resource exposes. Enforcement is two layers, both inside the daemon, both before `sh` spawns:

1. **Static parse.** Daemon parses the body as `/bin/sh`, walks the AST, collects every `simple_command` head, checks against the allowlist. Anything else → reject with a structured error naming the offender. Catches the obvious (`rm -rf $HOME`).
2. **PATH pinning + symlink farm.** Per call, daemon creates `/var/holo/run/<call-id>/bin/` with symlinks to only the allowed binaries. Runs the body with `PATH=/var/holo/run/<call-id>/bin` and a stripped env (`unset SHELL HOME`, reset `PATH`). Catches runtime composition (`$(echo ffprobe) "$f"`) that static parse misses.

Sandbox-exec / seccomp (rejecting absolute-path invocations like `/usr/bin/curl`) is a third layer, **deferred to v2**. Static parse + PATH pinning raise the bar enough that the residual surface is "what can you do *inside* the language runtimes you were granted." If `caps=exec:python3` is on the list, the owner is implicitly trusting that principal not to `__import__("os").system("/usr/bin/curl evil")`. Document this tradeoff explicitly per resource.

### The `readonly` modifier

`caps=…,readonly` flags the resource as not-meant-to-be-written:

- **Linux:** overlayfs over `$HOLO_RESOURCE_PATH`, upper layer discarded post-exec. Real isolation.
- **Darwin (primary v1 target):** snapshot `find $HOLO_RESOURCE_PATH -newer <call-start>` after the body exits. Any hits → log `readonly-violated` event, flag the call as audit-failed in the response. Convention-level, not enforcement. Fine because the principal is already trusted to *call* exec; `readonly` catches accidents, not adversaries.

### What's not in v1

- `caps=*` wildcard. Owner declares the surface explicitly.
- Per-principal cap overrides ("Alice gets `ffprobe`, Bob also gets `python3`"). Doubles config surface and test matrix without a known use case.
- Granular syscall/network ACL. If `curl` is allowed, exfil is in scope; granularity is at the binary level.

---

## Q3 — Path scoping

**Decision: three declarative isolation levels per resource; L1 default, L2 opt-in, L3 deferred to v2.**

### L1 — cwd-pin + static absolute-path rejection (default)

Body runs as the daemon-owner UID with `cwd=$HOLO_RESOURCE_PATH`. The static parse from Q2 gets one addition: reject body args that are absolute paths or contain `..`. So `cat /etc/passwd` is denied even if `cat` were allowlisted; `find . -name '*.mp4'` works. Language-runtime composition (`python3 -c 'open("/etc/passwd")…'`) still possible — the principal-trust tradeoff baked into granting `exec:python3` at all.

Error format on rejection (for ergonomics): `path-out-of-scope: '/tmp/output.mp4' — bodies must use paths relative to $HOLO_RESOURCE_PATH`. Specific enough to save confused bug reports.

### L2 — dedicated per-resource UID (opt-in)

Declared via `caps=…,uid:holo-resource-NAME`. Daemon `setuid`s to a dedicated unprivileged user that owns only `$HOLO_RESOURCE_PATH`. Cross-resource read isolation becomes Unix-classic strong.

Operational cost: daemon must have setuid privilege — runs as root, or with a configured `sudo`/`polkit` rule. That's a real ask, which is why it's opt-in. The realistic v1 user is "me and my six Macs at home"; defense against accidents matters more than defense against malicious peers.

### L3 — sandbox-exec / bubblewrap (v2)

Declared via `caps=…,sandbox`. Kernel-level path scope:

- **Darwin:** sandbox-exec profile generated per call. Default deny; allow read+write inside `$HOLO_RESOURCE_PATH`; allow read of `/usr/bin/*` and `/usr/lib/*` for allowed binaries' dyld chains; deny network if `readonly`.
- **Linux:** bubblewrap with equivalent profile.

Brittle per-platform implementation, dylib-chain bookkeeping, **not in v1**. The design hook (the `sandbox` cap declaration) is in v1 so the YAML schema doesn't change later; the *enforcement* is v2.

---

## Q4 — Result streaming model

**Decision: three modes selected by selector suffix; default = raw line-atomic concat; stderr always host-tagged; combined exit = max; `$ON_HOSTS` array for per-host introspection; `else` block fires once per failed host.**

### Three modes

| Suffix | Behavior |
|---|---|
| (default) | Each host's stdout streams to controller stdout as data arrives, line-atomic per host. Order non-deterministic across hosts. Scripts self-tag via `$HOLO_HOST` if attribution matters. |
| `:tagged` | Daemon prefixes each line with `host\t` before delivery. For free-form text. Caution: shifts column indexes for downstream awk pipelines. |
| `:json` | JSONL framing: `{"host":"…","resource":"…","fd":"stdout","line":"<original>"}` per line. For machine-readable pipes. |

No `:batched` mode — collect-then-deliver defeats progress visibility for long fan-outs.

Mode goes in the selector suffix, same family as the timeout: `on [holo:tag=video-files:5m:tagged] '…' /done`. The selector suffix takes a timeout and/or a mode separated by colons. Document this in the language reference.

### Stderr is always host-tagged

There's no shell-natural way to attribute an error to a host without a prefix, so stderr **always** prepends `host:` regardless of mode. Asymmetric on purpose: stdout is data (caller's responsibility to self-tag if needed), stderr is human-facing diagnostics.

### Exit semantics

- **`on` block's combined exit** = max of per-host exit codes. `if on [holo:…] '…'; then` reads as "all hosts succeeded."
- **`$ON_HOSTS` array** populated after the block: `(nas-01:0 studio-mac:0 lab-imac:1 …)`. Per-host introspection without parsing output.
- **`else` block fires once per failed host**, with `$HOLO_HOST` rebound to each. Failed = timeout, tunnel down, daemon rejection (auth/allowlist/scope), body exited non-zero.

---

## Q5 — Tag/resource record location

**Decision: tag union in TXT for cheap filter; full per-resource record over HTTP behind the existing token-auth + an MCP method `holo_list_resources` for the tai dispatch path.**

### What TXT carries

Three new keys appended to the existing `_holo-session._tcp.local.` TXT record:

| Key | Value | Purpose |
|---|---|---|
| `r=` | `video-files,photos,docs` | Flat union of all resource tags on this daemon. Cheap LAN-level tag filter. |
| `rn=` | `movies,family-photos,papers` | Flat union of resource names. Lets `on {holo:host=X,resource=movies}` pre-filter without HTTP. |
| `rcount=` | `3` | Total resource count. Optional. |

Total cost: ~200-500 bytes at realistic scales (8 resources, 5-20 tags). Comfortable inside the ~1300-byte TXT envelope.

### What HTTP serves

Full per-resource records at a new sub-path on the existing `--announce-capabilities` endpoint:

```
GET /v1/resources    (bearer token, same auth as capabilities)
→ {
    "resources": [
      {
        "name":  "movies",
        "path":  "/Volumes/movies",
        "tags":  ["video-files", "archive"],
        "caps":  ["exec:ffprobe", "exec:python3", "readonly"]
      }
    ]
  }
```

`path` and `caps` are sensitive (paths leak filesystem structure; caps leak the exec surface). HTTP-token-gated disclosure: the cert-authorized peer sees them; LAN browsers see only the tag/name union from TXT.

### What MCP exposes

A new MCP method `holo_list_resources` on the daemon proxies the same data as `GET /v1/resources` over the already-open MCP channel. Tai dispatch uses MCP (the auto-tunnel is already authed and warm — no point opening a parallel HTTPS connection to the capabilities endpoint). The HTTP surface is for non-tai consumers: future companion-app SPAs, debug CLIs, ad-hoc curl.

### Selector resolution, end to end

For `on [holo:tag=video-files:5m] '…'`:

1. mDNS browse `_holo-session._tcp.local.` — all sessions.
2. Filter on TXT `r=…video-files…` — drop daemons that don't advertise any video-files resource.
3. For each survivor: reuse the open MCP channel (or open one), call `holo_list_resources`, filter to resources whose `tags` array contains `video-files`.
4. Final dispatch list = `(host, resource, path)` tuples; one `holo_exec_in_resource` call per tuple.

### Future stealth mode

The TXT additions leak "this daemon has *some* video-files resource" — fine for the home-LAN single-user case (mDNS already leaks more), but multi-tenant or office deployments may want to omit the resource hints entirely. Reserved for v2 via `--announce-stealth`: when stealth, `r/rn/rcount` are absent from TXT and discovery falls back to "HTTP-fetch every reachable session, filter client-side." v1 design must not paint into a corner that prevents this.

---

## Q6 — Discovery cadence inside a tai script

**Decision: long-lived zeroconf subscription, lazy-opened on first `on`, hot for shell lifetime; first call blocks for `:settle=1s` (default), subsequent calls O(1); dispatch-anyway with timeout on marginal hosts.**

### How it works

First `on` call in a tai script triggers:

1. Tai's embedded Python opens a zeroconf browser subscribed to `_holo-session._tcp.local.` events.
2. Settle window: 1 second default. Overridable per call: `:settle=500ms` to dispatch sooner, `:settle=0` for "use whatever's currently visible" (might be empty on first call).
3. Subscription stays open for the shell's lifetime. Hosts coming up are picked up by the next `on`. Hosts going down silently age out per RFC 6762 (~120 seconds).

Subsequent `on` calls are O(1) — they read the live cache, no browse needed.

### Host-down semantics

When the subscription thinks a host is alive but it isn't (mid-script crash, network blip), `on` dispatches anyway. The dispatch times out, `else` fires, `$HOLO_HOST` is bound to that host. Silently skipping marginal hosts is harder to reason about than a visible timeout in `else`.

### Explicit refresh

One built-in: `holo_refresh_discovery` forces a re-browse without waiting for natural events. Blocks for `:settle`. For scripts that just spun up a new host and want to dispatch to it immediately. Optional convenience.

### Lifecycle

Subscription is lazy-init'd on first `on`, not at shell startup — a tai shell that never calls `on` pays nothing. Closes cleanly via an atexit handler in tai's embedded Python.

---

## Tai-side surface — the `on` keyword

The keyword is **`on`**, not `@`. `@` stays reserved for AI agent dispatch ("prompt → reply"). Overloading `@` for shell-on-remote would make scripts ambiguous to read at a glance — the two semantics deserve visually distinct constructs. `@@` was the runner-up but lingers in the `@` family; `dispatch` was ruled out because the codebase already uses "dispatch" terminology for `@` itself (`agent_dispatch.c`, `pool-dispatch-operator.md`).

Grammar is reused verbatim from `@`:

```
on [holo:tag=video-files:5m] '…body…' /done else { … }   # broadcast
on {holo:host=nas-01:30s} '…body…' /done                 # single target
on {holo:host=nas-01:5m:json} '…' /done                  # with mode suffix
```

- `[…]` = broadcast; `{…}` = single target.
- Selector predicates: `holo:tag=…`, `holo:host=…`, `holo:resource=…`, comma-joined for AND.
- Suffix `:Nm` / `:Ns` / `:Nh` = timeout. `:settle=Nms` = discovery settle override. `:tagged` / `:json` = output mode. All optional, in any order.
- `/sentinel` = remote process exit (matches the existing `@` sentinel grammar).
- `else { … }` block fires once per failed host with `$HOLO_HOST` rebound each time.

Per-call env injected by tai into the body's environment:

- `$HOLO_HOST` — host that received this dispatch
- `$HOLO_RESOURCE` — resource name
- `$HOLO_RESOURCE_PATH` — absolute path of the resource on that host (= `cwd`)

Post-block env populated by tai:

- `$ON_HOSTS` — array of `host:exit_code` pairs in completion order

Mild collision risk: `on` could shadow a user-defined function or alias of the same name. When the language reference lands, flag this so users with existing `on()` helpers can rename before upgrading.

---

## Out of scope for v1

- Remote UI automation via the exec primitive (`screen_*`, `browser_*`, `ui_template_*` stay local-only).
- Tai stdlib of common fan-out patterns.
- Holo-side write helpers (`holo_trash`, `holo_link`, `holo_move`).
- L3 path scoping (sandbox-exec / bubblewrap) — design hook present, enforcement v2.
- `caps=*` wildcard, per-principal cap overrides, granular syscall/network ACL.
- `--announce-stealth` mode — design must not paint into a corner that prevents it; enforcement v2.
- Per-`on` fresh mDNS browse — superseded by long-lived subscription.
- External `holo discover --serve` as the discovery cache backend — supersession only if the in-process subscription proves inadequate.

## Open implementation details

These are decisions deferred to the implementation phase, not Phase 0 — but flagged here so they don't ambush the implementer.

1. **Principal grammar + the multi-principal enforcement story.** Phase 2.B investigation confirmed: current holo certs encode a single fixed principal (`lando`) and provide no per-call identity at the MCP layer. So v1 ships `allow_principals` as a *declarative* surface — parsed into `Resource.allow_principals`, served from `/v1/resources` and `holo_list_resources`, but **not enforced.** Real enforcement needs one of: (a) backend signs certs with per-user principals + CloudCity sshd `AuthorizedPrincipalsCommand` maps principal → resources (auth at the SSH layer, before MCP), or (b) plumb `SSH_USER_AUTH`-style info from sshd → holo daemon → MCP per-call (auth at the MCP layer). Option (b) keeps the YAML/TOML as the source of truth; option (a) moves authority to the backend. Either is a multi-week project that's out of scope for Phase 2.B but in scope for a future v2 phase. Until then, the cert chain on the MCP channel is the only gate that fires.
2. **Bash AST parser inside the daemon.** Daemon is Python; options are `bashlex` (maintained but not heavily) or a hand-rolled tokenizer. Either is fine; confirm dependency choice when Phase 2 starts. The parser doesn't need to handle every bash edge case — it only needs to enumerate `simple_command` heads and detect absolute-path / `..` args.
3. **Per-resource UID lifecycle (L2).** Who creates `holo-resource-NAME` users? Manual setup, or a `holo resources init` subcommand that provisions them on Darwin (`sysadminctl`) and Linux (`useradd`)? Either is fine; the answer doesn't affect the L2 contract.
4. **MCP channel multiplexing.** `holo_exec_in_resource` is a streaming call. If a single channel handles multiple concurrent in-flight exec streams (script does `parallel` over per-host calls), the framing needs request IDs. MCP already supports this; just confirm.
5. **`holo discover --resource-tag X` CLI.** Phase 1 ships this as the human-facing test of the announce flow. Output format: TSV by default, `--json` for machine consumption. Matches existing `holo discover` style.
6. **Examples directory.** Ship `holo/examples/resources/` with the dedup-videos `fingerprint.tai` + `dedup.tai` pair once the implementation lands. Cross-link from this doc.

## Phase plan reference

This doc covers Phase 0 lock-in. The full phase plan lives in conversation context until it's written up:

| Phase | Scope | Depends on |
|---|---|---|
| 0 | Design lock-in (this doc) | — |
| 1 | Announce side, holo only | 0 |
| 2 | `holo_exec_in_resource` MCP tool + ACL + allowlist enforcement | 0 |
| 3 | `on` keyword + `holo:` selector namespace + MCP dispatch in tai | 1, 2 |
| 4 | Env binding + result streaming + `$ON_HOSTS` | 3 |
| 5 | Examples + docs (cross-link from `tai/docs/pool-dispatch-operator.md`) | 4 |

Critical path: 0 → (1 ∥ 2) → 3 → 4 → 5.
