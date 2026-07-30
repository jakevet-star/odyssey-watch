/*
 * seatcheck.js -- Aug 20 centre-pair check.
 *
 * HARDENED 2026-07-30 after three failures in one morning, all of them SILENT:
 *   1. Discovery scraped <a> tags. Harkins stopped rendering them on hydration
 *      and discovery returned 0 showtimes while reporting success.
 *   2. Seat reads were wrapped in a bare catch, so "0 seats found" and "could
 *      not read the page at all" looked identical in the logs.
 *   3. Jake's home IP got silently throttled: full HTTP 200s, complete JS
 *      bundle, but the ticketing SPA hangs forever on a spinner.
 *
 * The lesson is that a watcher which cannot distinguish "nothing available"
 * from "I am blind" is worse than no watcher, because it manufactures false
 * confidence. Everything below is built so failure is loud.
 *
 * WHAT JAKE WANTS
 *   rows G..M, 2 adjacent seats, both within 5 of row centre (seat 18 in a
 *   35-wide row). Row J is the pick; J17+J18 wins outright if free.
 *   Front rows put the frame centre 35-41 deg above eyeline for 172 minutes in
 *   seats that do not recline, which is why B-F are excluded entirely.
 */

const { chromium } = require('playwright');
const { execFile } = require('child_process');
const fs = require('fs');

const THEATRE = '16';
const MOVIE = 'HO00014201';
const DATE = process.env.DATE || '2026-08-20';
const WANT_TIMES = (process.env.WANT_TIMES || '3:30 PM,7:15 PM,11:00 PM')
  .split(',').map((s) => s.trim().toUpperCase()).filter(Boolean);
const WANTED_ROWS = ['G', 'H', 'J', 'K', 'L', 'M'];
const ROW_RANK = ['J', 'H', 'K', 'G', 'L', 'M'];
const ROW_CENTRE = { M: 16 };
const MAX_OFF_CENTRE = 5;
const MIN_SEATS = 2;
const DREAM_ROW = 'J';
const DREAM_SEATS = [17, 18];
const TOPIC = process.env.NTFY_TOPIC;
const HEALTH_FILE = process.env.HEALTH_FILE || '/tmp/seatcheck-health.json';

const centreOf = (r) => ROW_CENTRE[r] || 18;
const UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
           '(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36';

function push(title, body, url) {
  if (!TOPIC) { console.log('NTFY_TOPIC not set, skipping push'); return; }
  for (let i = 1; i <= 5; i++) {
    setTimeout(() => {
      execFile('curl', ['-s', '-X', 'POST', `https://ntfy.sh/${TOPIC}`,
        '-H', `Title: ${title} (${i}/5)`, '-H', 'Priority: urgent',
        '-H', 'Tags: clapper', '-H', `Click: ${url}`, '-d', body], () => {});
    }, (i - 1) * 20000);
  }
}

/* ---- discovery: parse embedded JSON, never scrape <a> tags ---------------
 * Harkins server-renders the full session list as JSON then strips the anchors
 * during hydration. The JSON also carries showtimeDate, a 70mm flag and a
 * soldOut flag, none of which the DOM ever exposed. */
function labelFor(iso) {
  const m = String(iso).match(/T(\d{2}):(\d{2})/);
  if (!m) return '';
  let h = parseInt(m[1], 10);
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12; if (h === 0) h = 12;
  return `${h}:${m[2]} ${ampm}`;
}

async function discover(date) {
  const res = await fetch(`https://www.harkins.com/movies/the-odyssey/${date}`, {
    headers: { 'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9' },
    redirect: 'follow',
  });
  if (!res.ok) throw new Error(`discovery HTTP ${res.status}`);
  const html = await res.text();
  const re = new RegExp(
    '\\{"theatreId":16,.*?"ticketingUrl":"[^"]*?/movie/(HO\\d+)/session/(\\d+)/date/(' + date + ')"[^}]*\\}', 'g');
  const out = [];
  let m;
  while ((m = re.exec(html)) !== null) {
    if (m[1] !== MOVIE) continue;
    const blob = m[0], id = m[2];
    const when = (blob.match(/"showtimeDate":"([^"]+)"/) || [])[1] || '';
    const seventy = (blob.match(/"seventymm":(\d)/) || [])[1];
    const soldOut = /"soldOut":true/.test(blob);
    const time = labelFor(when);
    if (seventy !== '1' || !time) continue;
    if (WANT_TIMES.length && !WANT_TIMES.includes(time.toUpperCase())) continue;
    out.push({ id, date, time, soldOut });
  }
  return out;
}

/* ---- seat reading: two independent strategies ---------------------------
 * Primary is the React fiber, because the DOM genuinely carries no
 * availability signal (every seat is an identical <button>). The fallback
 * reads aria-labels, which at least distinguishes "page rendered but I could
 * not parse it" from "page never rendered". */
async function readSeats(page, s) {
  const url = `https://harkins.com/ticketing/theatre/${THEATRE}/movie/${MOVIE}/session/${s.id}/date/${s.date}`;
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  await page.waitForSelector('button[aria-label^="Select Seat"]', { timeout: 25000 });
  await page.waitForTimeout(1200);

  const viaFiber = await page.evaluate(() => {
    const el = document.querySelector('button[aria-label^="Select Seat"]');
    const fk = el && Object.keys(el).find((k) => k.startsWith('__reactFiber'));
    if (!fk) return null;
    let n = el[fk], d = 0, rows = null;
    while (n && d < 25) {
      if (n.memoizedProps && Array.isArray(n.memoizedProps.rows)) { rows = n.memoizedProps.rows; break; }
      n = n.return; d++;
    }
    if (!rows) return null;
    return rows.filter((r) => r.seats && r.seats.length).map((r) => ({
      row: r.physicalName || r.name || '?',
      open: r.seats.filter((x) => x.status === 0).map((x) => x.id),   // 0 = available
    }));
  });
  if (viaFiber && viaFiber.length) return { rows: viaFiber, how: 'fiber' };

  // Fallback: aria-labels. Less reliable but proves the page rendered.
  const viaAria = await page.evaluate(() => {
    const btns = [...document.querySelectorAll('button[aria-label^="Select Seat"]')];
    if (!btns.length) return null;
    const byRow = {};
    for (const b of btns) {
      const m = (b.getAttribute('aria-label') || '').match(/([A-Z]+)[- ]?(\d+)/);
      if (!m) continue;
      const disabled = b.disabled || b.getAttribute('aria-disabled') === 'true';
      if (disabled) continue;
      (byRow[m[1]] = byRow[m[1]] || []).push(parseInt(m[2], 10));
    }
    return Object.entries(byRow).map(([row, open]) => ({ row, open }));
  });
  if (viaAria && viaAria.length) return { rows: viaAria, how: 'aria-fallback' };

  throw new Error('page rendered but no seat state could be parsed');
}

function bestPick(rows) {
  const out = [];
  for (const r of rows) {
    if (!WANTED_ROWS.includes(r.row) || r.open.length < MIN_SEATS) continue;
    const c = centreOf(r.row);
    const ids = [...new Set(r.open)].sort((a, b) => a - b);
    const runs = []; let run = [ids[0]];
    for (let i = 1; i < ids.length; i++) {
      if (ids[i] === ids[i - 1] + 1) run.push(ids[i]); else { runs.push(run); run = [ids[i]]; }
    }
    runs.push(run);
    for (const rn of runs) {
      if (rn.length < MIN_SEATS) continue;
      let best = null;
      for (let i = 0; i + MIN_SEATS <= rn.length; i++) {
        const win = rn.slice(i, i + MIN_SEATS);
        const dream = r.row === DREAM_ROW && win.length === 2 &&
                      win[0] === DREAM_SEATS[0] && win[1] === DREAM_SEATS[1];
        if (dream) { best = { win, worst: 0, dream: true }; break; }
        const worst = Math.max(...win.map((x) => Math.abs(x - c)));
        if (!best || worst < best.worst) best = { win, worst };
      }
      if (!best || best.worst > MAX_OFF_CENTRE) continue;
      out.push({ row: r.row, seats: best.win, off: best.worst, dream: !!best.dream,
                 rank: ROW_RANK.indexOf(r.row) < 0 ? 99 : ROW_RANK.indexOf(r.row) });
    }
  }
  out.sort((a, b) => (b.dream - a.dream) || a.rank - b.rank || a.off - b.off);
  return out[0] || null;
}

(async () => {
  const health = { ts: new Date().toISOString(), date: DATE, discovered: 0,
                   read: 0, failed: 0, blind: false, errors: [], hits: 0 };
  let sessions = [];
  try {
    sessions = await discover(DATE);
    health.discovered = sessions.length;
    console.log(`${DATE}: ${sessions.length} matching 70mm showtimes -> ` +
                sessions.map((s) => `${s.time}${s.soldOut ? ' (SOLD OUT)' : ''}`).join(', '));
  } catch (e) {
    health.errors.push(`discovery: ${e.message}`);
    console.log(`DISCOVERY FAILED: ${e.message}`);
  }

  if (!sessions.length) {
    health.blind = true;
    fs.writeFileSync(HEALTH_FILE, JSON.stringify(health, null, 1));
    console.log('NO SESSIONS DISCOVERED - treating as blind, not as "no seats"');
    process.exit(0);
  }

  const browser = await chromium.launch();
  const ctx = await browser.newContext({ userAgent: UA, viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const hits = [];

  for (const s of sessions) {
    const url = `https://harkins.com/ticketing/theatre/${THEATRE}/movie/${MOVIE}/session/${s.id}/date/${s.date}`;
    try {
      const { rows, how } = await readSeats(page, s);
      health.read++;
      const pick = bestPick(rows);
      const total = rows.reduce((a, r) => a + r.open.length, 0);
      if (pick) {
        console.log(`  ${s.time}: HIT row ${pick.row} seats ${pick.seats.join('+')}` +
                    `${pick.dream ? '  <-- J17+J18, the pick' : ''}  [${how}]`);
        hits.push({ time: s.time, pick, url });
      } else {
        console.log(`  ${s.time}: ${total} open, none in G-M centre  [${how}]`);
      }
    } catch (e) {
      health.failed++;
      const msg = e.message.split('\n')[0];
      health.errors.push(`${s.time}: ${msg}`);
      console.log(`  ${s.time}: READ FAILED - ${msg}`);
    }
  }
  await browser.close();

  // Every read failed => we are blind, which is NOT the same as "no seats".
  health.blind = health.read === 0 && sessions.length > 0;
  health.hits = hits.length;
  fs.writeFileSync(HEALTH_FILE, JSON.stringify(health, null, 1));

  if (health.blind) {
    console.log(`\n!! BLIND: all ${sessions.length} seat reads failed. ` +
                `This is NOT a "no seats" result.`);
    process.exit(0);
  }

  if (!hits.length) { console.log('\nno centre pairs (read OK).'); return; }

  hits.sort((a, b) => (b.pick.dream - a.pick.dream) || a.pick.rank - b.pick.rank);
  const h = hits[0];
  const seats = h.pick.seats.join(' + ');
  push(`${h.pick.dream ? 'J17+J18 OPEN' : 'BACK ROW'} - Aug 20 ${h.time} - Row ${h.pick.row}`,
    `Thu Aug 20, ${h.time}\nRow ${h.pick.row}, seats ${seats}\nHarkins Arizona Mills\n\n` +
    `Tap to book. Guest checkout is fine.\n\n(GitHub Actions - laptop can be closed)`,
    h.url);
  console.log(`\nALERT: Aug 20 ${h.time} row ${h.pick.row} ${seats}`);
  console.log(h.url);
  await new Promise((r) => setTimeout(r, 95000));   // let the staggered pushes fire
})();
