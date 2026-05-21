# Remote Captcha Solving

When CardScanner's headless Playwright browser hits an eBay captcha or bot
check, it can notify you and let you solve it remotely from your phone.

## How It Works

```
1. Playwright fetches an eBay search page for comp lookup
2. eBay returns a captcha/block page instead of results
3. CardScanner detects the block (checks for known signals in the HTML)
4. Server takes a screenshot → pushes it to your phone via ntfy
5. You open the /captcha page on your phone or browser
6. You click on the captcha to solve it (clicks are relayed to the real browser)
7. Server detects the block cleared → returns real eBay results
8. Comp lookup continues normally
```

The server waits up to **180 seconds** for the captcha to be solved before
giving up. If it times out, the comp lookup for that card fails gracefully
(card is still saved, just without pricing data).

## Setup

### 1. Configure ntfy

Add to `.env`:

```env
NTFY_TOPIC=cardscanner-connor
NTFY_SERVER=https://ntfy.sh
```

### 2. Install the ntfy app

- **iOS:** [App Store](https://apps.apple.com/us/app/ntfy/id1625396347)
- **Android:** [Play Store](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
  or [F-Droid](https://f-droid.org/packages/io.heckel.ntfy/)

### 3. Subscribe to your topic

Open the ntfy app → tap "+" → enter `cardscanner-connor` (or your `NTFY_TOPIC`).

Enable notifications for this topic.

## Solving a Captcha

When you receive a push notification:

1. **Tap the notification** — it links directly to `http://<server>:8765/captcha`
2. **Look at the screenshot** — it shows what the browser sees
3. **Click on the captcha** — your clicks are mapped to the actual browser coordinates
4. **Hit "Refresh Screenshot"** to see the updated state
5. **Repeat** until the captcha is solved

The page auto-checks every 5 seconds whether a captcha is still pending.

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/captcha` | GET | HTML page for remote solving |
| `/captcha/status` | GET | `{"waiting": bool, "url": string}` |
| `/captcha/screenshot` | GET | PNG screenshot of the captcha page |
| `/captcha/click` | POST | `{"x": int, "y": int}` — click at coordinates |
| `/captcha/type` | POST | `{"text": string}` — type text into the page |

## When Captchas Happen

eBay's bot detection (Akamai) triggers captchas when:

- The Playwright profile doesn't have established eBay cookies yet
- Too many searches in a short window
- The IP or fingerprint changes (common with Docker restarts)

### Reducing captcha frequency

1. **Use eBay Browse API instead** — set `EBAY_ENV=production` with valid
   production API keys. The Browse API doesn't trigger captchas at all.
   This is the permanent fix.
2. **Persistent Playwright profile** — the Docker volume keeps the browser
   profile across restarts at `/data/playwright-profile`. Solved captchas
   stay solved.
3. **eBay session warmup** — the code automatically visits `ebay.com` before
   search pages to look like a real browsing session.

## Disabling Notifications

Remove or leave blank `NTFY_TOPIC` in `.env`. The captcha solving still works
at `/captcha` — you just won't get push notifications.

## Troubleshooting

### "No captcha right now" but comp lookups are failing

The captcha may have already timed out (180s window). Check the logs:

```bash
docker compose logs --tail 50 | grep -i captcha
```

### Clicks not registering

The screenshot is 1280x1000 — clicks are scaled from your screen to those
coordinates. If the captcha element is very small, try zooming in on your
phone browser first, then clicking.

### ntfy notifications not arriving

1. Check the topic matches between `.env` and the ntfy app
2. Check the ntfy app has notification permissions
3. Test manually: `curl -d "test" https://ntfy.sh/cardscanner-connor`
4. Check server logs for "ntfy notification failed"
