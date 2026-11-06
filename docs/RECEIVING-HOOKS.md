# Receiving the bot's events in n8n, Make or a CRM

Every event is a JSON POST to `HOOK_URL`:

```json
{"id": "evt_3f1c...", "type": "lead.captured", "occurred_at": 1794000000,
 "community": "Pixelforge Creators Club",
 "data": {"discord_user_id": "1300000000000009004", "username": "priya",
          "email": "priya@brushwork.example", "message": "...", "source": "discord /contact"}}
```

Event types: `member.joined`, `member.left`, `ticket.opened`, `ticket.closed`, `lead.captured`,
`premium.granted`, `premium.removed`, `booking.created`, `booking.canceled`. `hooks.events` in
`config/bot.yaml` limits which are sent.

Headers:

| Header | Meaning |
| --- | --- |
| `X-Community-Event` | the event type |
| `X-Community-Timestamp` | unix seconds when this attempt was sent |
| `X-Community-Signature` | `sha256=` + hex HMAC-SHA256 of `"<timestamp>.<raw body>"` with `HOOK_SIGNING_SECRET` |
| `Idempotency-Key` | the event id, the same on every retry |

Answer with any 2xx to accept. 429, 5xx and timeouts are retried with growing gaps (up to 8 tries);
any other 4xx parks the event as dead and it shows on the dashboard's audit page.

## Checking the signature in an n8n Code node

Turn on "Raw body" in the Webhook node, then:

```js
const crypto = require('crypto');
const ts = $input.first().json.headers['x-community-timestamp'];
const sig = $input.first().json.headers['x-community-signature'];
const raw = Buffer.from($input.first().binary.data.data, 'base64').toString();
const expected = 'sha256=' + crypto.createHmac('sha256', $env.HOOK_SIGNING_SECRET)
  .update(`${ts}.${raw}`).digest('hex');
if (sig !== expected || Math.abs(Date.now() / 1000 - Number(ts)) > 300) throw new Error('bad signature');
return [{ json: JSON.parse(raw) }];
```

Then dedupe on `Idempotency-Key` (a Data Store or a CRM lookup) before creating a contact or deal.
