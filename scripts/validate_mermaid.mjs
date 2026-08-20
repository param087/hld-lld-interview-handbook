#!/usr/bin/env node
/**
 * validate_mermaid.mjs — render-test every Mermaid diagram with the same mermaid@11 build
 * that Material for MkDocs loads in the browser.
 *
 * Usage:
 *   node scripts/validate_mermaid.mjs [--files a.md b.md ...] [--docs docs]
 *                                     [--site site] [--port 8765] [--screens DIR]
 *
 * Phase 1 (always): extract every ```mermaid fence from the given files (default: all .md
 *   under --docs except _templates/), run `mermaid.parse` AND `mermaid.render` on each in a
 *   headless browser and report `file:line: <error>` for the ones that fail.
 * Phase 2 (--site DIR): serve the built site with `python -m http.server`, visit every page
 *   from sitemap.xml, and check that each `<pre class="mermaid">` in the static HTML became a
 *   rendered `div.mermaid`; collect page errors; screenshot failing pages into --screens DIR.
 *
 * Environment:
 *   PLAYWRIGHT_MODULE  path to playwright's index.mjs (default: global nvm install, 1.55)
 *   PW_CHANNEL         browser channel, default "chrome"; empty string = bundled chromium
 *   PYTHON             python used for http.server (default: .venv/bin/python)
 *   VALIDATE_MERMAID_VERBOSE  set to print per-page timings (phase 2) on stderr
 *
 * Output ends with `validate_mermaid: B source blocks, F failures[, P pages rendered]`;
 * exit status 1 iff failures. Network access to unpkg.com is required.
 */

import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const MERMAID_URL = 'https://unpkg.com/mermaid@11/dist/mermaid.min.js';
const PW =
  process.env.PLAYWRIGHT_MODULE ||
  '/Users/param/.nvm/versions/node/v23.6.1/lib/node_modules/playwright/index.mjs';
const CHANNEL = process.env.PW_CHANNEL === undefined ? 'chrome' : process.env.PW_CHANNEL;
const PYTHON = process.env.PYTHON || join(ROOT, '.venv', 'bin', 'python');
const RENDER_TIMEOUT_MS = 20_000;
const VERBOSE = Boolean(process.env.VALIDATE_MERMAID_VERBOSE);

// ---------------------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------------------
function usage(msg) {
  if (msg) console.error(`validate_mermaid: ${msg}`);
  console.error(
    'usage: node scripts/validate_mermaid.mjs [--files a.md ...] [--docs docs] [--site site] [--port 8765] [--screens DIR]',
  );
  process.exit(2);
}

function parseArgs(argv) {
  const opts = { files: [], docs: 'docs', site: null, port: 8765, screens: null };
  let current = null;
  for (const arg of argv) {
    if (arg.startsWith('--')) {
      current = arg.slice(2);
      if (!Object.hasOwn(opts, current)) usage(`unknown option ${arg}`);
      continue;
    }
    if (current === null) usage(`unexpected argument ${arg}`);
    if (current === 'files') opts.files.push(arg);
    else if (current === 'port') opts.port = Number(arg);
    else opts[current] = arg;
    if (current !== 'files') current = null;
  }
  if (!Number.isInteger(opts.port) || opts.port <= 0) usage('--port must be a positive integer');
  return opts;
}

const abs = (p) => (isAbsolute(p) ? p : resolve(process.cwd(), p));
const show = (p) => {
  const rel = relative(ROOT, abs(p));
  return rel && !rel.startsWith('..') ? rel.split(sep).join('/') : p;
};
const firstLines = (text, n) => String(text).split('\n').filter(Boolean).slice(0, n).join(' | ');

// ---------------------------------------------------------------------------------------
// Phase 1: source blocks
// ---------------------------------------------------------------------------------------
function walkMarkdown(dir, out = []) {
  for (const name of readdirSync(dir).sort()) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name !== '_templates') walkMarkdown(p, out);
    } else if (name.endsWith('.md')) {
      out.push(p);
    }
  }
  return out;
}

const FENCE_OPEN = /^([ \t]*)(`{3,}|~{3,})(.*)$/;
const FENCE_CLOSE = /^[ \t]*(`{3,}|~{3,})[ \t]*$/;

/** Extract ```mermaid fences (any indentation; nested fences inside other fences are ignored). */
function extractBlocks(file) {
  const lines = readFileSync(file, 'utf8').split(/\r?\n/);
  const blocks = [];
  let fence = null;
  lines.forEach((line, idx) => {
    if (fence) {
      const close = line.match(FENCE_CLOSE);
      if (close && close[1][0] === fence.marker[0] && close[1].length >= fence.marker.length) {
        if (fence.lang === 'mermaid') {
          blocks.push({ file, line: fence.start, src: fence.body.join('\n') });
        }
        fence = null;
        return;
      }
      let strip = 0;
      while (strip < fence.indent.length && strip < line.length && (line[strip] === ' ' || line[strip] === '\t')) {
        strip += 1;
      }
      fence.body.push(line.slice(strip));
      return;
    }
    const open = line.match(FENCE_OPEN);
    if (open && !(open[2][0] === '`' && open[3].includes('`'))) {
      const info = open[3].trim();
      const lang = info.startsWith('{')
        ? (info.replace(/[{}]/g, '').split(/\s+/).find((t) => t.startsWith('.')) || '.').slice(1)
        : info.split(/\s+/)[0] || '';
      fence = { marker: open[2], indent: open[1], lang, start: idx + 1, body: [] };
    }
  });
  return blocks;
}

async function phase1(browser, blocks) {
  const failures = [];
  if (blocks.length === 0) return { failures, ms: 0 };
  const page = await browser.newPage();
  page.setDefaultTimeout(60_000);
  await page.goto('about:blank');
  await page.setContent('<html><body></body></html>');
  await page.addScriptTag({ url: MERMAID_URL });
  await page.evaluate(() => window.mermaid.initialize({ startOnLoad: false, securityLevel: 'loose' }));
  const t0 = Date.now();
  for (const [i, block] of blocks.entries()) {
    const error = await page.evaluate(
      async ({ id, src }) => {
        try {
          await window.mermaid.parse(src);
          await window.mermaid.render(id, src);
          return null;
        } catch (e) {
          return String((e && (e.message || e.str)) || e);
        } finally {
          document.getElementById(id)?.remove();
          document.getElementById(`d${id}`)?.remove();
        }
      },
      { id: `m${i}`, src: block.src },
    );
    if (error) failures.push(`${show(block.file)}:${block.line}: ${firstLines(error, 3)}`);
  }
  const ms = Date.now() - t0;
  await page.close();
  return { failures, ms };
}

// ---------------------------------------------------------------------------------------
// Phase 2: built site
// ---------------------------------------------------------------------------------------
async function startServer(site, port) {
  const child = spawn(
    PYTHON,
    ['-m', 'http.server', '--bind', '127.0.0.1', '--directory', site, String(port)],
    { stdio: 'ignore' },
  );
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i += 1) {
    if (child.exitCode !== null) throw new Error(`http.server exited early with status ${child.exitCode}`);
    try {
      const res = await fetch(`${base}/`);
      if (res.status < 500) return { child, base };
    } catch {
      /* not up yet */
    }
    await sleep(100);
  }
  child.kill();
  throw new Error('http.server did not become reachable within 10 s');
}

function sitePages(site) {
  const sitemap = join(site, 'sitemap.xml');
  if (existsSync(sitemap)) {
    const locs = [...readFileSync(sitemap, 'utf8').matchAll(/<loc>\s*([^<\s]+)\s*<\/loc>/g)].map((m) => m[1]);
    return locs.map((loc) => {
      try {
        return new URL(loc).pathname;
      } catch {
        return loc.startsWith('/') ? loc : `/${loc}`;
      }
    });
  }
  const pages = [];
  const walk = (dir) => {
    for (const name of readdirSync(dir).sort()) {
      const p = join(dir, name);
      if (statSync(p).isDirectory()) walk(p);
      else if (name === 'index.html') pages.push(`/${relative(site, dir).split(sep).join('/')}/`.replace('//', '/'));
    }
  };
  walk(site);
  return pages;
}

const slug = (path) => path.replace(/^\/|\/$/g, '').replace(/[^A-Za-z0-9_-]+/g, '_') || 'index';

const countMermaid = () => ({
  pre: document.querySelectorAll('pre.mermaid').length,
  div: document.querySelectorAll('div.mermaid').length,
});

/** Poll the page until every <pre class="mermaid"> became a div.mermaid, or the timeout passes. */
async function pollCounts(page, expected, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let counts = await page.evaluate(countMermaid);
  while ((counts.pre !== 0 || counts.div < expected) && Date.now() < deadline) {
    await sleep(200);
    counts = await page.evaluate(countMermaid);
  }
  return counts;
}

async function phase2(browser, site, port, screens) {
  const failures = [];
  let rendered = 0;
  const { child, base } = await startServer(site, port);
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    page.setDefaultTimeout(30_000);
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String((e && e.message) || e)));
    for (const path of sitePages(site)) {
      pageErrors.length = 0;
      const url = base + path;
      const started = Date.now();
      let problem = null;
      try {
        const html = await (await fetch(url)).text();
        const expected = (html.match(/<pre class="mermaid">/g) || []).length;
        const response = await page.goto(url, { waitUntil: 'load' });
        if (!response || !response.ok()) {
          problem = `HTTP ${response ? response.status() : 'no response'}`;
        } else if (expected > 0) {
          // Material removes the class on mount, re-adds it while mermaid loads, then replaces the
          // <pre> with a div.mermaid holding a shadow root; a failed render leaves the <pre> behind.
          // Poll from here rather than page.waitForFunction: a timed-out in-page waiter makes
          // Chrome take 10-50 s to shut down afterwards.
          const counts = await pollCounts(page, expected, RENDER_TIMEOUT_MS);
          if (counts.pre !== 0 || counts.div !== expected) {
            problem = `expected ${expected} rendered diagram(s), found ${counts.div} (unrendered <pre class="mermaid">: ${counts.pre}) after ${RENDER_TIMEOUT_MS / 1000} s`;
          }
        }
      } catch (e) {
        problem = firstLines((e && e.message) || e, 1);
      }
      if (pageErrors.length) {
        problem = `${problem ? `${problem}; ` : ''}page errors: ${pageErrors.slice(0, 3).map((m) => firstLines(m, 1)).join(' | ')}`;
      }
      rendered += 1;
      let shot = '';
      if (problem) {
        failures.push(`${path}: ${problem}`);
        if (screens) {
          const t = Date.now();
          mkdirSync(screens, { recursive: true });
          await page
            .screenshot({ path: join(screens, `${slug(path)}.png`), fullPage: true, timeout: 10_000 })
            .catch((e) => console.error(`  screenshot of ${path} failed: ${firstLines(e.message || e, 1)}`));
          shot = ` (screenshot ${Date.now() - t} ms)`;
        }
      }
      if (VERBOSE) console.error(`  ${path}: ${Date.now() - started} ms${problem ? ' FAIL' : ''}${shot}`);
    }
    const tCtx = Date.now();
    await context.close();
    if (VERBOSE) console.error(`  context closed in ${Date.now() - tCtx} ms`);
  } finally {
    child.kill();
  }
  return { failures, rendered };
}

// ---------------------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------------------
/** Close the browser, but never hang on it: kill the process if it takes more than 5 s. */
async function closeBrowser(browser) {
  let done = false;
  await Promise.race([
    browser.close().catch(() => {}).finally(() => {
      done = true;
    }),
    sleep(5_000),
  ]);
  if (!done) {
    console.error('validate_mermaid: browser did not close within 5 s; killing it');
    browser.process()?.kill('SIGKILL');
  }
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const files = opts.files.length ? opts.files.map(abs) : walkMarkdown(abs(opts.docs));
  for (const f of files) if (!existsSync(f)) usage(`no such file: ${f}`);
  if (opts.site && !existsSync(opts.site)) usage(`no such site directory: ${opts.site}`);
  const blocks = files.flatMap(extractBlocks);

  let playwright;
  try {
    playwright = await import(pathToFileURL(PW).href);
  } catch (e) {
    console.error(`validate_mermaid: cannot import Playwright from ${PW} (set PLAYWRIGHT_MODULE): ${e.message}`);
    process.exit(2);
  }
  const tLaunch = Date.now();
  const browser = await playwright.chromium.launch({ headless: true, channel: CHANNEL || undefined });
  if (VERBOSE) console.error(`browser launched in ${Date.now() - tLaunch} ms (channel: ${CHANNEL || 'bundled chromium'})`);
  const failures = [];
  let pages = null;
  try {
    const tSetup = Date.now();
    const p1 = await phase1(browser, blocks);
    if (VERBOSE) console.error(`phase 1 incl. mermaid download: ${Date.now() - tSetup} ms`);
    failures.push(...p1.failures);
    if (blocks.length) {
      console.error(`phase 1: ${blocks.length} blocks in ${p1.ms} ms (${(p1.ms / blocks.length).toFixed(0)} ms/diagram)`);
    }
    if (opts.site) {
      const t0 = Date.now();
      const p2 = await phase2(browser, abs(opts.site), opts.port, opts.screens ? abs(opts.screens) : null);
      failures.push(...p2.failures);
      pages = p2.rendered;
      console.error(`phase 2: ${pages} pages in ${Date.now() - t0} ms`);
    }
  } finally {
    const tClose = Date.now();
    await closeBrowser(browser);
    if (VERBOSE) console.error(`browser closed in ${Date.now() - tClose} ms`);
  }
  for (const line of failures) console.log(line);
  const tail = pages === null ? '' : `, ${pages} pages rendered`;
  console.log(`validate_mermaid: ${blocks.length} source blocks, ${failures.length} failures${tail}`);
  process.exit(failures.length ? 1 : 0);
}

main().catch((e) => {
  console.error(`validate_mermaid: ${e && e.stack ? e.stack : e}`);
  process.exit(2);
});
