/*
 * seatcheck.js -- Aug 20 centre-pair check, for GitHub Actions.
 *
 * Reads seat availability out of Harkins' React state. The DOM is useless here:
 * every seat is an identical <button> whether free or sold. Each seat object
 * carries a numeric `status`, and 0 means AVAILABLE. Verified against the
 * rendered map three independent ways on 2026-07-28.
 *
 * Requirements, matching what Jake actually wants:
 *   - rows G..M only (front rows mean 35-41 deg of neck extension for 172 min
 *     in seats that do not recline)
 *   - 2 adjacent seats
 *   - both within 5 of the row centre (seat 18 in a 35-wide row); the screen
 *     is not strongly curved, so off-axis seats get real keystone
 *   - showtimes he can actually reach: he lands Aug 20 at 12:42pm
 */

const { chromium } = require('playwright');
const { execFile } = require('child_process');

const THEATRE = '16';
const MOVIE = 'HO00014201';
const DATE = process.env.DATE || '2026-08-20';
const WANT_TIMES = (process.env.WANT_TIMES || '3:30 PM,7:15 PM,11:00 PM')
  .split(',').map((s) => s.trim().toUpperCase());
const WANTED_ROWS = ['G', 'H', 'J', 'K', 'L', 'M'];
const ROW_RANK = ['J', 'H', 'K', 'G', 'L', 'M'];     // J is the pick
const ROW_CENTRE = { M: 16 };                         // M is narrower than 35
const MAX_OFF_CENTRE = 5;
const MIN_SEATS = 2;
const TOPIC = process.env.NTFY_TOPIC;

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

function bestPick(rows) {
  const out = [];
  for (const r of rows) {
    if (!WANTED_ROWS.includes(r.row) || r.open.length < MIN_SEATS) continue;
    const c = centreOf(r.row);
    const ids = [...r.open].sort((a, b) => a - b);
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
        const worst = Math.max(...win.map((s) => Math.abs(s - c)));
        if (!best || worst < best.worst) best = { win, worst };
      }
      if (!best || best.worst > MAX_OFF_CENTRE) continue;   // flanks: reject
      out.push({ row: r.row, seats: best.win, off: best.worst,
                 rank: ROW_RANK.indexOf(r.row) < 0 ? 99 : ROW_RANK.indexOf(r.row) });
    }
  }
  out.sort((a, b) => a.rank - b.rank || a.off - b.off);
  return out[0] || null;
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ userAgent: UA });
  const page = await ctx.newPage();
  const hits = [];

  try {
    // Find this date's sessions. Harkins server-renders the links, and the
    // anchor text is the showtime.
    await page.goto(`https://harkins.com/movies/the-odyssey/${DATE}`,
      { waitUntil: 'domcontentloaded', timeout: 45000 });
    await page.waitForTimeout(3500);

    const sessions = await page.evaluate((movie) =>
      [...document.querySelectorAll('a[href*="/ticketing/theatre/16/"]')]
        .map((a) => ({ href: a.getAttribute('href') || '', text: a.textContent.trim() }))
        .filter((x) => x.href.includes(`/movie/${movie}/`)), MOVIE);

    const wanted = sessions.filter((s) => {
      const m = s.href.match(/session\/(\d+)\/date\/(\d{4}-\d{2}-\d{2})/);
      if (!m || m[2] !== DATE) return false;
      return WANT_TIMES.some((w) => s.text.toUpperCase().replace(/\s+/g, ' ').includes(w));
    });

    console.log(`${DATE}: ${wanted.length} matching showtimes -> ${wanted.map((w) => w.text).join(', ')}`);

    for (const s of wanted) {
      const id = s.href.match(/session\/(\d+)/)[1];
      const url = `https://harkins.com/ticketing/theatre/${THEATRE}/movie/${MOVIE}/session/${id}/date/${DATE}`;
      try {
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForSelector('button[aria-label^="Select Seat"]', { timeout: 25000 });
        await page.waitForTimeout(1200);
        const rows = await page.evaluate(() => {
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
            open: r.seats.filter((x) => x.status === 0).map((x) => x.id),  // 0 = available
          }));
        });
        if (!rows) { console.log(`  ${s.text}: could not read seat state`); continue; }
        const pick = bestPick(rows);
        const total = rows.reduce((a, r) => a + r.open.length, 0);
        if (pick) {
          console.log(`  ${s.text}: HIT row ${pick.row} seats ${pick.seats.join('+')}`);
          hits.push({ time: s.text, pick, url });
        } else {
          console.log(`  ${s.text}: ${total} open, none in G-M centre`);
        }
      } catch (e) {
        console.log(`  ${s.text}: ${e.message.split('\n')[0]}`);
      }
    }
  } finally {
    await browser.close();
  }

  if (!hits.length) { console.log('no centre pairs. done.'); return; }

  hits.sort((a, b) => a.pick.rank - b.pick.rank);
  const h = hits[0];
  const seats = h.pick.seats.join(' + ');
  push(`BACK ROW - Aug 20 ${h.time} - Row ${h.pick.row}`,
    `Thu Aug 20, ${h.time}\nRow ${h.pick.row}, seats ${seats}\nHarkins Arizona Mills\n\n` +
    `Tap to book. Guest checkout is fine.\n\n(from GitHub Actions - laptop can be closed)`,
    h.url);
  console.log(`ALERT: Aug 20 ${h.time} row ${h.pick.row} ${seats}`);
  console.log(h.url);
  // keep the process alive long enough for the staggered pushes to fire
  await new Promise((r) => setTimeout(r, 95000));
})();
