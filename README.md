# hermes-ntfy-notify

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that sends an [ntfy](https://ntfy.sh) push notification
when a Hermes **WebUI** conversation turn finishes (or fails).

Hermes' bundled `ntfy` platform plugin covers cron deliveries, but WebUI turns go through the
gateway `api_server` platform and never reach it. This plugin hooks `post_llm_call` to fill that gap.

## Features

- Title = profile name + start of your message; body = short plain-text preview of the reply.
- Tap opens the session in the Hermes Android app (`hermes://session/<id>`); an "Open (web)" button links to the WebUI.
- Failure notification (priority 4) when a turn ends without a final answer.
- Interrupted turns (`/stop`) stay silent; optional minimum duration to skip short turns.
- Fire-and-forget in a daemon thread with a 4 s timeout: ntfy being down never slows Hermes.
- Per-profile configuration (scoped secrets), so each Hermes profile can use its own token.

## Install

```bash
cp -r hermes-ntfy-notify "$HERMES_HOME/plugins/ntfy-notify"
hermes plugins validate "$HERMES_HOME/plugins/ntfy-notify"
hermes plugins enable ntfy-notify
```

Add to the profile `.env` and restart the gateway:

| Variable | Meaning |
|---|---|
| `NTFY_NOTIFY_URL` | ntfy server URL reachable from the Hermes container |
| `NTFY_NOTIFY_TOKEN` | token of an ntfy user allowed to publish on the topic |
| `NTFY_NOTIFY_TOPIC` | topic, default `hermes-turns` |
| `NTFY_NOTIFY_WEBUI_URL` | public HTTPS base of the WebUI, for the web button |
| `NTFY_NOTIFY_MIN_SECONDS` | skip turns shorter than N seconds (default `0`) |

`touch DRYRUN` next to `__init__.py` logs notifications to `agent.log` instead of sending them (no restart needed).

## Known issue: tap shows "json error not found" in the Android app

The Hermes Android app turns `hermes://session/<id>` into `GET /<id>` on the WebUI instead of
`/session/<id>`, which the WebUI answers with a JSON 404. Changing the link format does not help
(the app keeps only the last path segment). Workaround: redirect `/<id>` to `/session/<id>` on the
reverse proxy. Example Cloudflare Single Redirect (works on the free plan, no regex):

- When: `http.host eq "hermes.example.com" and len(http.request.uri.path) eq 13 and not substring(http.request.uri.path, 1) contains "/" and not http.request.uri.path contains "."`
- Then: dynamic 302 to `concat("https://hermes.example.com/session/", substring(http.request.uri.path, 1))`

(Session ids are 12 hex characters, hence the length of 13 with the leading slash.)

## License

MIT
