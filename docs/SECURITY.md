# Security model

Briefer processes fully-untrusted input (anything anyone forwards it) and
runs on a server you care about. The design goal: **the bot must not be
usable as a foothold into the server, and must not act on instructions
hidden in the content it analyses.**

## Threat model
- **Untrusted message content** (text, captions, file contents, image
  pixels) — may contain prompt-injection payloads or malicious markup.
- **Untrusted URLs** — may point at internal services (SSRF) or huge/hostile
  responses.
- **Unauthorized users** — random people finding the bot.
- **Secret exposure** — token / API key / password leaking via logs or repo.

## Controls

### 1. Authentication & authorization
- **Allow-list**: `ALLOWED_CHAT_IDS` — messages from any other chat are
  silently dropped (logged, never answered). Prevents discovery/abuse.
- **Login password**: a chat must `/login <password>` (PBKDF2-SHA256,
  200k iterations, per-process random salt). Only the hash is held in
  memory; the plaintext password is never persisted by the running bot and
  the `/login` message is auto-deleted from chat history.
- **Bootstrap mode** is the only bypass and must be turned off in
  production (`BRIEFER_BOOTSTRAP=0`).

### 2. No code execution from input
User content is never passed to a shell, `eval`, `exec`, `pickle`, or a
template engine. Uploaded binaries of unknown type are **noted, not
parsed/executed**. All SQL uses parameterised queries.

### 3. Prompt-injection resistance
Every model call is prefixed with a guard instructing the model to treat
forwarded material as *data to analyse, never instructions to follow*, and to
flag injection attempts. The verification pass is adversarial and independent.

### 4. SSRF protection (`security.safe_resolve` + `enrich._fetch`)
Before **every** hop the target host is resolved and rejected if it maps to a
loopback, private, link-local, multicast, reserved, or unspecified address
(blocks `169.254.169.254` cloud metadata, `localhost`, `10/8`, `192.168/16`,
`*.internal`, …). Only `http`/`https` and only ports `80`/`443`.
- **Redirects are followed manually** with `follow_redirects=False`; each
  `Location` is re-validated *before* a socket is opened, so an open-redirect
  to an internal address never issues the internal request.
- **DNS-rebinding is closed by IP pinning on direct egress**: `safe_resolve`
  returns a concrete validated public IP and `_fetch` connects to *that* IP,
  keeping the `Host` header and TLS SNI/certificate check on the real
  hostname — so a host that resolves to a public IP during validation cannot
  resolve to `127.0.0.1`/metadata during the connect.
- When egress is via an HTTP proxy (the proxy owns DNS + policy), pinning is
  skipped and the request is tunnelled normally; per-hop validation still
  applies.
- Downloads are size-capped (`MAX_DOWNLOAD_BYTES`) and redirect count bounded.

### 5. Resource / abuse limits
Per-chat token-bucket rate limiting (`RATE_LIMIT_PER_MINUTE`), max text
length, max download size, PDF page cap.

### 6. Host hardening (systemd unit)
Runs as a dedicated **non-login system user**, with:
`NoNewPrivileges`, `ProtectSystem=strict` (only `data/` writable),
`ProtectHome`, `PrivateTmp`, `ProtectKernel*`, `RestrictNamespaces`,
`MemoryDenyWriteExecute`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`,
`SystemCallFilter=@system-service`, empty `CapabilityBoundingSet`, `UMask=0077`.

### 7. Secret hygiene
`.env` and `service_account.json` are git-ignored and `chmod 600`. Logs run
through a redaction filter that masks Telegram tokens and API keys. Nothing
secret is echoed to Telegram.

## Operational recommendations
- Put the server behind a firewall; the bot only needs **outbound** HTTPS
  (Telegram, Anthropic, Google, and the sites you send it). No inbound port
  is opened — it uses long-polling, not webhooks.
- Rotate the bot token / API key if you suspect exposure; then
  `./manage.sh restart`.
- Review the `Verified` column in the sheets: `⚠️` rows had at least one
  claim the verifier couldn't confirm — treat their deadlines/criteria with
  care.
- Keep `BRIEFER_BOOTSTRAP=0` except during initial chat-id discovery.
