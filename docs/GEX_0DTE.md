# SPY 0DTE dealer gamma (getgamma.io)

The Super Signals desk shows a `GEX 0DTE` line:

```
GEX 0DTE  SPY NEG  net -$12.56B  flip 743.09  call wall 740  put wall 730  magnets 744-733
```

It refreshes from the database every 5 minutes and carries its own
"updated Nm ago" stamp. Reading it never touches the vendor.

## Where the numbers come from

`GET https://www.getgamma.io/api/options?ticker=SPY&mode=0dte&strikes=50`
returns the **raw chain** — per-contract `greeks.gamma`, `open_interest`,
`strike_price` — and getgamma's own dashboard computes the headline figures in
the browser. So `app/services/gex0dte.py` computes them here:

| field | how |
| --- | --- |
| net GEX | `Σ (gamma × OI × 100 × spot² × 0.01)`, calls positive, puts negative |
| flip | where the **cumulative** net crosses zero, interpolated between the bracketing strikes |
| call wall | strike carrying the most **call** gamma exposure |
| put wall | strike carrying the most **put** gamma exposure |
| magnets | the heaviest absolute-gamma strikes bracketing spot |

Walls are ranked by gamma **exposure**, not open interest — a fat-OI strike
with negligible gamma is not a wall.

## No credentials

The endpoint needs none. Verified 2026-07-30 with a session JWT, with only the
`gamma_fp` visitor cookie, and with no cookies at all: all three answer
identically. Nothing is stored and nothing expires.

## Why the server cannot fetch it

getgamma sits behind Vercel bot protection. A server-side request gets
**HTTP 429 + a "Security Checkpoint" page** whatever headers or cookies it
carries; a real browser on the site gets 200. That is client fingerprinting,
and this project does not try to defeat it. The app's own page cannot fetch it
either — no CORS headers.

So the working path is to read the chain **in a tab that is already allowed**
and push it to the API. Pressing `⟳ update 0DTE` still tries the direct fetch
first, and explains this if it is blocked.

## Why `⟳ update 0DTE` cannot mint a snapshot on its own

The button calls the API immediately — nothing waits for the schedule. The
API is what cannot finish: its request to getgamma is answered with the bot
check, so there is no fresh chain to compute from. It therefore re-reads the
stored snapshot, which keeps the card on the newest data held rather than
going stale or blank, and the age only resets when a genuinely new snapshot
lands.

To make a click (or a 5-minute tick) actually produce new data, the fetch has
to happen where it is allowed: a tab on getgamma.io. Use the auto-push
bookmarklet below — click it once when the session opens and that tab keeps
pushing on its own, so the desk is always current and the button becomes a
convenience rather than the mechanism.

## The auto-push bookmarklet (recommended)

Same as the one-shot below, but it keeps pushing every 5 minutes until the tab
is closed or you click it again. Click once after the open; the desk then
tracks it for the whole session.

```js
javascript:(()=>{if(!/(^|\.)getgamma\.io$/.test(location.hostname)){alert('Vidura 0DTE: run this ON the getgamma dashboard tab.\n\nThis tab is '+location.hostname+', where /api/options is not the option chain.');return;}if(window.__vidPush){clearInterval(window.__vidPush);window.__vidPush=null;alert('Vidura 0DTE auto-push STOPPED');return;}const go=async(loud)=>{try{const r=await fetch('/api/options?ticker=SPY&mode=0dte&strikes=50',{credentials:'include'});const ct=r.headers.get('content-type')||'';if(!ct.includes('json')){if(loud)alert('Vidura 0DTE: getgamma answered HTTP '+r.status+' with a page, not the chain. Reload the dashboard tab and click again.');return;}const d=await r.json();const p={ticker:d.ticker,spotPrice:d.spotPrice,mode:d.mode,timestamp:d.timestamp,marketStatus:d.marketStatus,marketOpen:d.marketOpen,contracts:d.contracts.map(c=>({contract_type:c.contract_type,strike_price:c.strike_price,open_interest:c.open_interest,greeks:{gamma:c.greeks&&c.greeks.gamma}}))};const q=await fetch('http://localhost:8790/api/v1/super/gex0dte/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({payload:p})});const v=await q.json();if(loud)alert(q.ok?('Vidura 0DTE auto-push STARTED (every 5 min)\n\n'+v.note):('Push failed: '+(v.detail||q.status)));}catch(e){if(loud)alert('Vidura 0DTE failed: '+e);}};go(true);window.__vidPush=setInterval(()=>go(false),300000);})()
```

Clicking it a second time in the same tab stops the loop.

It runs against whichever tab is FOCUSED, not the getgamma one — so if it
is clicked while another tab is in front, `/api/options` resolves against
that site and returns HTML. The hostname check above catches that and says
which tab it landed on, instead of failing with a JSON parse error.


Clicking it a second time stops the loop.

## The one-shot bookmarklet

Make a bookmark whose URL is the line below, open
<https://www.getgamma.io/dashboard>, and click it. It reads the chain in that
tab and posts it to the local API, which computes and stores the view; the
desk picks it up on its next 5-minute poll (or a page refresh).

```js
javascript:(async()=>{try{const r=await fetch('/api/options?ticker=SPY&mode=0dte&strikes=50',{credentials:'include'});const d=await r.json();const p={ticker:d.ticker,spotPrice:d.spotPrice,mode:d.mode,timestamp:d.timestamp,marketStatus:d.marketStatus,marketOpen:d.marketOpen,contracts:d.contracts.map(c=>({contract_type:c.contract_type,strike_price:c.strike_price,open_interest:c.open_interest,greeks:{gamma:c.greeks&&c.greeks.gamma}}))};const q=await fetch('http://localhost:8790/api/v1/super/gex0dte/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({payload:p})});const v=await q.json();alert(q.ok?('Vidura 0DTE updated\n\n'+v.note):('Push failed: '+(v.detail||q.status)));}catch(e){alert('Vidura 0DTE failed: '+e);}})()
```

Only derived fields are sent — contract type, strike, open interest and gamma.
No cookies leave the browser.

## Endpoints

| method | path | notes |
| --- | --- | --- |
| `GET` | `/api/v1/super/gex0dte` | stored view + `fetched_at`; 404 before the first refresh |
| `POST` | `/api/v1/super/gex0dte/refresh` | `{}` tries the vendor; `{"payload": …}` accepts a captured chain |

Snapshots persist in `daily_snapshots` under `kind='gex0dte'`, one row per day,
so the desk survives an API restart.
