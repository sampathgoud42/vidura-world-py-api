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

## The pusher bookmarklet

One bookmark. Make its URL the line below, open
<https://www.getgamma.io/dashboard>, and click it once per session.

```js
javascript:(()=>{if(!/(^|\.)getgamma\.io$/.test(location.hostname)){alert('Vidura 0DTE: run this ON the getgamma dashboard tab.\n\nThis tab is '+location.hostname+'.');return;}var W=window,P=300000,MAX=1800000,API='http://localhost:8790/api/v1/super/gex0dte/';if(W.__vid){try{W.__vid.dead=1;clearInterval(W.__vid.h);if(W.__vid.rel)W.__vid.rel();if(W.__vid.el)W.__vid.el.remove();if(W.__vid.t0)document.title=W.__vid.t0;}catch(e){}}var S={seq:0,okAt:0,tryAt:0,err:'',back:P,due:0,dead:0,h:null,el:null,rel:null,t0:document.title,sid:Math.random().toString(36).slice(2,8)};W.__vid=S;var el=document.createElement('div');S.el=el;el.style.cssText='position:fixed;right:10px;bottom:10px;z-index:2147483647;font:11px/1.4 Consolas,monospace;white-space:pre-wrap;background:rgb(13,17,23);color:rgb(201,209,217);border:1px solid rgb(48,54,61);border-radius:6px;padding:6px 8px;max-width:300px;opacity:.93';var tx=document.createElement('div'),bs=document.createElement('button');bs.textContent='stop';bs.style.cssText='margin-top:5px;font:10px Consolas,monospace;cursor:pointer';el.appendChild(tx);el.appendChild(bs);(document.body||document.documentElement).appendChild(el);var ago=t=>t?Math.round((Date.now()-t)/1000)+'s':'never';var paint=()=>{var a=S.okAt?Math.round((Date.now()-S.okAt)/1000):-1;el.style.borderColor=(a<0||a>660)?'rgb(248,81,73)':(S.err?'rgb(210,153,34)':'rgb(35,134,54)');tx.textContent='VID 0DTE '+S.sid+' cycle '+S.seq+'\nlast ok  '+ago(S.okAt)+'\nlast try '+ago(S.tryAt)+'\nevery '+Math.round(S.back/1000)+'s'+(S.err?'\n! '+S.err:'');try{document.title=(a<0?'[x --]':(a>660?'[! '+a+'s]':'[o '+a+'s]'))+' '+S.t0;}catch(e){}};var post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});var beat=r=>{try{post(API+'heartbeat',{session:S.sid,seq:S.seq,ok:!r,reason:r||'',wall:Date.now(),mono:Math.round(performance.now())}).catch(()=>{});}catch(e){}};var slow=()=>{S.back=Math.min(S.back*2,MAX);};var go=async()=>{if(S.dead)return;S.seq++;S.tryAt=Date.now();paint();try{var r=await fetch('/api/options?ticker=SPY&mode=0dte&strikes=50',{credentials:'include',cache:'no-store'});var ct=r.headers.get('content-type')||'';if(!ct.includes('json')){S.err='vendor HTTP '+r.status+' sent a page, not JSON'+(r.redirected?' (redirected)':'')+' - reload this tab';slow();beat('vendor-nonjson-'+r.status);paint();return;}var d=await r.json();if(!r.ok||!d||!Array.isArray(d.contracts)||!d.contracts.length){S.err='vendor HTTP '+r.status+' empty/!ok chain';slow();beat('vendor-badchain-'+r.status);paint();return;}if(d.mode&&String(d.mode)!=='0dte'){S.err='vendor mode='+d.mode+', not 0dte - not pushing';slow();beat('vendor-mode-'+d.mode);paint();return;}var p={ticker:d.ticker,spotPrice:d.spotPrice,mode:d.mode,timestamp:d.timestamp,marketStatus:d.marketStatus,marketOpen:d.marketOpen,contracts:d.contracts.map(c=>({contract_type:c.contract_type,strike_price:c.strike_price,open_interest:c.open_interest,greeks:{gamma:c.greeks&&c.greeks.gamma}}))};var q=await post(API+'refresh',{payload:p,client:{session:S.sid,seq:S.seq,wall:Date.now(),mono:Math.round(performance.now())}});if(!q.ok){var v={};try{v=await q.json();}catch(e){}S.err='desk HTTP '+q.status+' '+(v.detail||'');slow();beat('desk-'+q.status);paint();return;}S.okAt=Date.now();S.err='';S.back=P;beat('');}catch(e){S.err='network/CORS: '+e;slow();beat('net');}paint();};var tick=()=>{if(S.dead)return;paint();if(Date.now()>=S.due){S.due=Date.now()+S.back;go();}};var kick=()=>{if(!S.dead&&Date.now()-S.tryAt>20000){S.due=0;tick();}};S.h=setInterval(tick,15000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)kick();});document.addEventListener('resume',kick);W.addEventListener('focus',kick);W.addEventListener('pageshow',kick);W.addEventListener('online',kick);try{if(navigator.locks)navigator.locks.request('vidura0dte',{mode:'exclusive'},()=>new Promise(res=>{S.rel=res;}));}catch(e){}S.stop=()=>{S.dead=1;clearInterval(S.h);if(S.rel)S.rel();if(S.el)S.el.remove();try{document.title=S.t0;}catch(e){}};bs.onclick=S.stop;S.due=0;tick();})()
```

Clicking it again always **restarts** it — it tears down any previous loop
first. There is deliberately no click-to-stop: the old bookmarklet toggled off
on a second click, so the natural reaction to a stale card (click it again)
was the one action that guaranteed it stayed stale. To stop it, press `stop`
on the badge.

Only derived fields are sent — contract type, strike, open interest and gamma.
Verified by test: every other field on the vendor's contract objects is
dropped before the POST. No cookies leave the browser.

### What it does that the old one did not

| behaviour | why |
| --- | --- |
| on-page badge, bottom-right | liveness is readable **without clicking**, and clicking is what used to kill it. It shows the session id, cycle number, age of the last success, age of the last attempt, current interval, and the last error |
| tab title prefix `[o 42s]` / `[! 900s]` / `[x --]` | the same signal from the tab strip, without switching to the tab |
| every failure is reported | the old loop ran `go(false)` and swallowed **every** error silently — non-JSON, empty chain, thrown `TypeError`, connection refused, 4xx, 5xx. That is why a stall could not be told apart from a working loop |
| a 15s supervisor tick with an explicit due-time | a missed or coalesced wake-up catches up immediately instead of re-anchoring its phase, and a long stall is detectable from inside the page |
| pushes on `visibilitychange` / `focus` / `pageshow` / `online` | the desk is fresh the moment you look at it, and a tab coming back from throttling resyncs at once |
| `r.ok`, array, non-empty and `mode === '0dte'` gates, plus `cache: 'no-store'` | the old code checked only content-type, so a JSON-bodied 429 reached `.contracts.map` and threw into the silent catch. A well-formed chain for the **wrong expiry** is worse than no chain: it would be stamped fresh |
| backoff 5m → 10m → 20m → 30m on failure, reset on success | stops hammering an edge that is already refusing you |
| a heartbeat POST every cycle, pass or fail | see below — this is what makes the desk able to say *why* |

### Known limits — visible, not fixed

Everything above lives inside the getgamma document and dies with it. A page
reload, a top-level navigation, a tab discard, a renderer crash, a browser
restart or closing the tab all destroy the loop, and no in-page code can
survive that. A bookmarklet is a one-shot; nothing re-injects it.

That is why the desk-side detection below exists: those cases cannot be
prevented here, so they have to be *visible* there.

Worth setting once, to make discards much less likely:
`chrome://settings/performance` → **Always keep these sites active** →
add `getgamma.io`.

## Telling a dead pusher from a blocked one

The snapshot tables are upserts — one row per day, one per hour — so the
database held no cadence at all. "The last push landed at 14:40" was a single
mutable field with nothing behind it, which is exactly why the 2026-07-30 stall
could not be resolved into *the tab died* versus *the tab was alive and every
push was refused*. Those need opposite responses, so they are now separate
signals.

`POST /api/v1/super/gex0dte/heartbeat` records one append-only row per push
cycle, pass or fail. It never fetches the vendor, never stores a snapshot and
never fills an hour slot — routing liveness through `/refresh` would make the
**server** call getgamma on every failed cycle, and would stamp a stalled feed
as fresh.

`GET /api/v1/super/gex0dte` then reports:

| field | meaning |
| --- | --- |
| `stale` | no data for >11 min while the 08:15–15:30 CST window is open |
| `pusher_state` | `pushing` · `blocked` (cycles arriving, all refused) · `dead` (no cycles) · `idle` (outside the window) · `unknown` |
| `pusher_reason` | the last failure, e.g. `vendor-nonjson-429`, `desk-422` |
| `pusher_seq` | cycle number; gaps mean the timer stopped, a reset to 1 means a new document |

The desk renders these as an **amber** `PUSHER BLOCKED` chip (alive but
refused — re-clicking will not help) or a **red** `PUSHER DEAD` chip (no
cycles — re-click the bookmarklet). Amber versus red is precisely the
distinction that was missing on 07/30.

## Diagnosing a stall in the tab

Paste this in the getgamma tab's console. Do it **before** looking at
`chrome://discards` — merely activating a discarded tab reloads it and erases
the evidence.

```js
(()=>{const n=performance.getEntriesByType('navigation')[0]||{};console.log({loop:!!window.__vid,dead:window.__vid&&window.__vid.dead,seq:window.__vid&&window.__vid.seq,err:window.__vid&&window.__vid.err,discarded:document.wasDiscarded,navType:n.type,pageAgeSec:Math.round(performance.now()/1000),hidden:document.hidden});fetch('/api/options?ticker=SPY&mode=0dte&strikes=50',{credentials:'include',cache:'no-store'}).then(r=>{console.log('vendor',r.status,r.headers.get('content-type'),'redirected',r.redirected);return r.text();}).then(t=>console.log('body head:',t.slice(0,200))).catch(e=>console.log('vendor threw',e));fetch('http://localhost:8790/api/v1/super/gex0dte').then(r=>r.json()).then(j=>console.log('desk',{fetched_at:j.fetched_at,age_seconds:j.age_seconds,stale:j.stale,pusher_state:j.pusher_state,pusher_reason:j.pusher_reason})).catch(e=>console.log('desk unreachable',e));})()
```

Read it as:

- `loop: true` — the document survived and the loop is alive; the failure is
  downstream, and `err` / the `vendor` line says which.
- `loop: false` with `discarded: true` — Chrome discarded the tab.
- `loop: false` with a small `pageAgeSec` — the page reloaded.
- `loop: false` with `pageAgeSec` spanning the whole session — the bookmarklet
  was never armed in this document.

`document.wasDiscarded` is the only supported signal; restore navigations do
not report `navType: 'reload'`, and there is no JS-readable lifecycle state.

## Why the server still cannot fetch it itself

getgamma sits behind Vercel bot protection. A server-side request gets
**HTTP 429 + a "Security Checkpoint" page** whatever headers or cookies it
carries; a real browser on the site gets 200. That is client fingerprinting,
and this project does not try to defeat it. The app's own page cannot fetch it
either — no CORS headers.

Pressing `⟳ update 0DTE` still tries the direct fetch first and explains this
if it is blocked. It cannot mint a snapshot on its own: the API's request is
answered with the bot check, so it re-reads the stored snapshot instead, and
the age only resets when a genuinely new snapshot lands.

## Private Network Access

The tab is a **public** `https://www.getgamma.io` page POSTing to a
**loopback** address, so Chrome sends an extra preflight carrying
`Access-Control-Request-Private-Network`. Starlette's `CORSMiddleware` does not
implement PNA and rejects that preflight outright with
`400 Disallowed CORS private-network` — which would kill the pusher silently.

`app/main.py` answers it, deliberately narrowly: PNA is the only thing standing
between `allow_origins=["*"]` and any site the user visits POSTing to a local
API that can start and stop trading bots. It is granted for exactly the
getgamma origins, on exactly `/gex0dte/refresh` and `/gex0dte/heartbeat`, and
nowhere else.

## Endpoints

| method | path | notes |
| --- | --- | --- |
| `GET` | `/api/v1/super/gex0dte` | stored view + `fetched_at`; 404 before the first refresh |
| `POST` | `/api/v1/super/gex0dte/refresh` | `{}` tries the vendor; `{"payload": …}` accepts a captured chain |
| `POST` | `/api/v1/super/gex0dte/heartbeat` | one append-only row per push cycle, pass or fail |
| `GET` | `/api/v1/super/gex0dte/history` | hourly net gamma 08:00-16:00 CST; `?date=YYYY-MM-DD` |
| `GET` | `/api/v1/super/gex0dte/history/dates` | days holding at least one captured hour |

Snapshots persist in `daily_snapshots` under `kind='gex0dte'`, one row per day,
so the desk survives an API restart.
