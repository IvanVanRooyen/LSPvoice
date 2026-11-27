"""Local admin dashboard for the voice agent: calls, reports, reviews, tests, settings, knowledge.

    python dashboard.py            opens http://127.0.0.1:8102 in your browser

Local only: it listens on 127.0.0.1 (not reachable through the Cloudflare tunnel), rejects any
other Host header, and every action needs a token generated when the dashboard starts. It
never shows credentials. Transcripts, reports and reviews come from the private folder.
"""
import asyncio
from datetime import datetime
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import secrets
import statistics
import sys
from urllib.request import urlopen
import webbrowser

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import configure
import flows as flow_engine
import harness
from knowledge import CORE_LIMIT, DELIVERY, LIBRARY, Library
import pilot_log
import review
import transcripts

HOST, PORT = '127.0.0.1', 8102
ALLOWED_HOSTS = {f'{HOST}:{PORT}', f'localhost:{PORT}'}
TOKEN = secrets.token_urlsafe(24)
HERE = Path(__file__).resolve().parent
PILOT_HEALTH = 'http://127.0.0.1:8101/health'
JOBS = {}  # job id -> {'title', 'command', 'started', 'lines', 'done', 'code'}

app = FastAPI(title='LSPcare voice agent dashboard', docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware('http')
async def local_only(request, call_next):
    # Blocks DNS-rebinding style access from web pages: only our own host names are served.
    if request.headers.get('host') not in ALLOWED_HOSTS:
        return HTMLResponse('Local dashboard only.', status_code=403)
    return await call_next(request)


def check_token(token):
    if not secrets.compare_digest(str(token or ''), TOKEN):
        raise HTTPException(403, 'Missing or invalid dashboard token; reload the page.')


# ------------------------------------------------------------------ rendering helpers

CSS = """
:root{--ink:#14213d;--muted:#5b6475;--line:#d9dee7;--soft:#f3f5f9;--teal:#0f8b8d;--teal-soft:#e3f3f3;--navy:#1f3a68;
--amber:#b26b00;--amber-soft:#fbf0dc;--red:#b23a3a;--red-soft:#fbe9e9;--green:#2e7d4f;--green-soft:#e4f2e9;--violet:#5b4b9a;--violet-soft:#ece9f6}
*{box-sizing:border-box}body{margin:0;font-family:"Segoe UI",Arial,sans-serif;color:var(--ink);background:#fafbfd;font-size:14px}
header{background:var(--navy);color:#fff;padding:10px 24px;display:flex;align-items:center;gap:28px;position:sticky;top:0;z-index:5}
header b{font-size:16px;letter-spacing:.02em}nav a{color:#c9d6ee;text-decoration:none;margin-right:18px;font-weight:600}
nav a.on,nav a:hover{color:#fff}main{padding:22px 28px;max-width:1280px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px}.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:14px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.card .big{font-size:24px;font-weight:700}.card .lbl{color:var(--muted);font-size:12px;margin-top:2px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden}
th{background:var(--soft);text-align:left;font-size:12px;color:var(--muted);padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}tr:last-child td{border-bottom:none}
a{color:var(--teal)}.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600}
.ok{background:var(--green-soft);color:var(--green)}.warn{background:var(--amber-soft);color:var(--amber)}
.bad{background:var(--red-soft);color:var(--red)}.info{background:var(--violet-soft);color:var(--violet)}.grey{background:var(--soft);color:var(--muted)}
.chat{display:flex;flex-direction:column;gap:8px;max-width:900px}
.b{padding:8px 12px;border-radius:10px;max-width:80%;line-height:1.4;white-space:pre-wrap}
.b small{display:block;font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;margin-bottom:2px}
.caller{background:#fff;border:1px solid var(--line);align-self:flex-start}.agent{background:var(--teal-soft);align-self:flex-end}
.tool{align-self:center;font-size:12px;background:var(--violet-soft);color:var(--violet);padding:3px 10px;border-radius:10px;max-width:90%}
.report{align-self:center;background:var(--red-soft);color:var(--red);font-weight:600;padding:6px 12px;border-radius:8px}
.sys{align-self:center;font-size:12px;color:var(--muted)}
form.inline{display:inline}button,.btn{background:var(--teal);color:#fff;border:0;border-radius:6px;padding:7px 14px;font-weight:600;cursor:pointer;font-size:13px;text-decoration:none;display:inline-block}
button.secondary{background:var(--navy)}input,select{padding:6px 8px;border:1px solid var(--line);border-radius:6px;font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}pre{background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px;white-space:pre-wrap;font-size:12px}
.log{background:#101828;color:#e4e7ec;border-radius:8px;padding:10px;font:12px Consolas,monospace;white-space:pre-wrap;min-height:120px}
details{background:#fff;border:1px solid var(--line);border-radius:8px;padding:8px 12px;margin:8px 0}summary{cursor:pointer;font-weight:600}
.flow{display:flex;flex-direction:column;align-items:center;gap:0;max-width:560px}.fstep{width:100%;border-radius:10px;padding:10px 14px;border:1px solid var(--line);background:#fff}.fstep.collect{border-left:5px solid var(--teal)}.fstep.confirm{border-left:5px solid var(--amber);background:var(--amber-soft)}.fstep.action{border-left:5px solid var(--violet);background:var(--violet-soft)}.fstep.end{text-align:center;background:var(--soft)}.arrow{color:var(--muted);font-size:18px;line-height:22px}.kind{font-size:11px;font-weight:700;text-transform:uppercase;color:var(--muted)}
.notice{background:var(--amber-soft);border:1px solid #efd7a6;border-radius:8px;padding:8px 12px;margin:10px 0}
"""

NAV = [('/', 'Overview'), ('/calls', 'Calls'), ('/reports', 'Reports'), ('/reviews', 'Reviews'),
       ('/tests', 'Tests'), ('/flows', 'Flows'), ('/settings', 'Settings'), ('/knowledge', 'Knowledge')]


def page(title, body, active='/'):
    nav = ''.join(f'<a href="{href}" class="{"on" if href == active else ""}">{name}</a>' for href, name in NAV)
    return HTMLResponse(f'<!doctype html><html lang="en-AU"><head><meta charset="utf-8"><title>{escape(title)} · LSPcare voice agent</title>'
                        f'<style>{CSS}</style></head><body><header><b>LSPcare voice agent</b><nav>{nav}</nav></header>'
                        f'<main>{body}</main></body></html>')


def e(value):
    return escape('' if value is None else str(value))


def token_field():
    return f'<input type="hidden" name="token" value="{TOKEN}">'


def when(iso):
    return e(str(iso or '')[:16].replace('T', ' '))


def markdown(text):
    """Small renderer for our own reports: headings, bullets, code blocks, bold, inline code."""
    out, in_code, in_list = [], False, False

    def inline(s):
        s = escape(s)
        s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)
        return re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
    for line in text.splitlines():
        if line.startswith('```'):
            if in_list:
                out.append('</ul>'); in_list = False
            out.append('</pre>' if in_code else '<pre>'); in_code = not in_code
            continue
        if in_code:
            out.append(escape(line)); continue
        if line.startswith('- '):
            if not in_list:
                out.append('<ul>'); in_list = True
            out.append(f'<li>{inline(line[2:])}</li>'); continue
        if in_list:
            out.append('</ul>'); in_list = False
        if line.startswith('#'):
            level = min(len(line) - len(line.lstrip('#')) + 1, 4)
            out.append(f'<h{level}>{inline(line.lstrip("#").strip())}</h{level}>')
        elif line.startswith('|'):
            out.append(f'<div class="muted">{inline(line)}</div>')
        elif line.strip():
            out.append(f'<p>{inline(line)}</p>')
    if in_list:
        out.append('</ul>')
    return '\n'.join(out)


# ------------------------------------------------------------------ data

def pilot_status():
    try:
        with urlopen(PILOT_HEALTH, timeout=1.5) as response:
            return json.load(response)
    except Exception:
        return None


def read_config():
    return json.loads(configure.CONFIG.read_text(encoding='utf-8')) if configure.CONFIG.is_file() else {}


def calls():
    reviewed = review._load_state()
    rows = []
    for path in reversed(transcripts._calls()):
        try:
            info = transcripts.summary(path)
        except (OSError, ValueError):
            continue
        info['review'] = reviewed.get(path.name)
        rows.append(info)
    return rows


def call_status(info):
    pills = []
    if info['flagged']:
        pills.append('<span class="pill bad">⚑ reported</span>')
    r = info.get('review')
    if r is None:
        pills.append('<span class="pill grey">not reviewed</span>')
    elif r['issues']:
        pills.append(f'<span class="pill warn">! {len(r["issues"])} issue{"s" if len(r["issues"]) != 1 else ""}</span>')
    else:
        pills.append('<span class="pill ok">✓ reviewed</span>')
    return ' '.join(pills)


def harness_runs():
    runs = []
    if not harness.RUNS.is_dir():
        return runs
    for folder in sorted(harness.RUNS.iterdir(), reverse=True):
        results = folder / 'results.json'
        if not results.is_file():
            continue
        data = json.loads(results.read_text(encoding='utf-8'))
        firsts = [t['first_word_ms'] for r in data['results'] for t in r['run']['turns'] if t.get('first_word_ms') is not None]
        runs.append({'name': folder.name, 'data': data, 'passed': sum(r['passed'] for r in data['results']),
                     'total': len(data['results']), 'median': statistics.median(firsts) if firsts else None})
    return runs


def safe_child(folder, name, pattern):
    if not re.fullmatch(pattern, name or ''):
        raise HTTPException(404)
    path = (folder / name).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.exists():
        raise HTTPException(404)
    return path


# ------------------------------------------------------------------ background jobs (tests, reviews)

async def run_job(job_id, command):
    job = JOBS[job_id]
    process = await asyncio.create_subprocess_exec(*command, cwd=str(HERE), stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.STDOUT,
                                                   env={**os.environ, 'PYTHONUTF8': '1'})
    async for raw in process.stdout:
        line = raw.decode('utf-8', errors='replace').rstrip()
        if line and not line.startswith('[pilot] turn'):
            job['lines'].append(line)
    job['code'] = await process.wait()
    job['done'] = True


def start_job(title, command):
    if any(not j['done'] for j in JOBS.values()):
        raise HTTPException(409, 'Another test or review is still running; wait for it to finish.')
    job_id = datetime.now().strftime('%H%M%S') + secrets.token_hex(2)
    JOBS[job_id] = {'title': title, 'command': command, 'started': datetime.now().isoformat(timespec='seconds'),
                    'lines': [], 'done': False, 'code': None}
    asyncio.get_running_loop().create_task(run_job(job_id, command))
    return job_id


# ------------------------------------------------------------------ pages

@app.get('/')
async def overview():
    status = await asyncio.to_thread(pilot_status)
    cfg = read_config()
    rows = calls()
    today = datetime.now().strftime('%Y-%m-%d')
    reports = pilot_log.read_reports()
    runs = harness_runs()
    unreviewed = sum(1 for r in rows if r['review'] is None and r['turns'])
    issues = sum(1 for r in rows if r['review'] and r['review']['issues'])
    options = None
    try:
        import claude_reply
        options = claude_reply.request_options(cfg) if cfg.get('provider') == 'anthropic' else None
    except ValueError:
        pass
    pilot = (f'<span class="pill ok">● answering calls</span> <span class="muted">tickets: {e(status.get("tickets"))} · '
             f'inventory: {"active" if status.get("inventory", {}).get("active") else "not loaded"}</span>'
             if status else '<span class="pill bad">● not running</span> <span class="muted">start it with run.ps1</span>')
    model = (f'{e(options["model"])} · effort {e(options["output_config"]["effort"])} · thinking '
             f'{e(options.get("thinking", {}).get("type", "adaptive"))}' if options else e(cfg.get('model')))
    last = runs[0] if runs else None
    body = f"""<h1>Overview</h1><p class="muted">Pilot {pilot}</p>
    <div class="cards">
      <div class="card"><div class="big">{sum(1 for r in rows if r['at'].startswith(today))}</div><div class="lbl">calls today · {len(rows)} saved</div></div>
      <div class="card"><div class="big" style="color:var(--red)">{len(reports)}</div><div class="lbl">problems reported by voice</div></div>
      <div class="card"><div class="big" style="color:var(--amber)">{issues}</div><div class="lbl">calls with review issues</div></div>
      <div class="card"><div class="big">{unreviewed}</div><div class="lbl">calls not yet reviewed</div></div>
      <div class="card"><div class="big">{f'{last["passed"]}/{last["total"]}' if last else '–'}</div><div class="lbl">last test run{f' · {last["name"][:8]}' if last else ''}</div></div>
    </div>
    <div class="grid2"><div class="card"><b>Agent</b><p class="muted">{model}<br>voice {e(cfg.get('voice'))} · transcripts {e(cfg.get('transcripts', 'off'))}
      · call reference {e(cfg.get('call_reference', 'on'))} · staff reports {e(cfg.get('staff_feedback', 'on'))}</p><a href="/settings">Change settings</a></div>
    <div class="card"><b>Next steps</b><p class="muted">{'Review the new calls. ' if unreviewed else ''}{'Look at the reported problems. ' if reports else ''}
      Turn confirmed problems into tests from the call page, fix them, then run the tests.</p>
      <form class="inline" method="post" action="/reviews/run">{token_field()}<button>Review new calls</button></form>
      <a class="btn" style="background:var(--navy)" href="/tests">Run tests</a></div></div>
    <h2>Latest calls</h2>{calls_table(rows[:8])}"""
    return page('Overview', body, '/')


def calls_table(rows):
    if not rows:
        return '<p class="muted">No saved calls yet. Calls are saved while <code>transcripts</code> is on.</p>'
    body = ''.join(f"""<tr><td><a href="/calls/{e(r['file'])}">{when(r['at'])}</a></td><td>{e(r['reference'] or '–')}</td>
        <td>…{e(r['caller'] or '???')}</td><td>{r['turns']}</td><td>{e(r['ended'] or '–')}</td><td>{call_status(r)}</td>
        <td class="muted">{e((r['version'] or {}).get('code', ''))}</td></tr>""" for r in rows)
    return f'<table><tr><th>When</th><th>Reference</th><th>Caller</th><th>Turns</th><th>Ended</th><th>Status</th><th>Version</th></tr>{body}</table>'


@app.get('/calls')
async def calls_page(q: str = '', show: str = 'all'):
    rows = calls()
    if q:
        rows = [r for r in rows if q.strip() in (r['reference'], r['caller'])]
    if show == 'attention':
        rows = [r for r in rows if r['flagged'] or (r['review'] and r['review']['issues'])]
    if show == 'unreviewed':
        rows = [r for r in rows if r['review'] is None]
    body = f"""<h1>Calls</h1><form method="get" style="margin:10px 0">
      <input name="q" value="{e(q)}" placeholder="Reference or caller digits"> <select name="show">
      {''.join(f'<option value="{v}" {"selected" if v == show else ""}>{t}</option>' for v, t in [('all', 'All calls'), ('attention', 'Needs attention'), ('unreviewed', 'Not reviewed')])}
      </select> <button class="secondary">Filter</button></form>{calls_table(rows)}"""
    return page('Calls', body, '/calls')


@app.get('/calls/{name}')
async def call_detail(name: str, made: str = ''):
    path = safe_child(transcripts.FOLDER, name, r'[\w.-]+\.jsonl')
    info = transcripts.summary(path)
    reviewed = review._load_state().get(path.name)
    v = info['version'] or {}
    parts = []
    for r in info['records']:
        kind = r.get('kind')
        if kind in ('greeting', 'agent'):
            parts.append(f'<div class="b agent"><small>Agent{" · greeting" if kind == "greeting" else ""}</small>{e(r["text"])}</div>')
        elif kind == 'caller':
            parts.append(f'<div class="b caller"><small>Caller · {when(r.get("at"))[11:]}</small>{e(r["text"])}</div>')
        elif kind == 'tool' and r.get('name') == 'report_problem':
            parts.append(f'<div class="report">⚑ Problem reported: {e(r.get("args", {}).get("description"))}</div>')
        elif kind == 'tool':
            parts.append(f'<div class="tool">⚙ {e(transcripts._tool_summary(r["name"], r.get("args", {}), r.get("result")))}</div>')
        elif kind == 'interrupted':
            parts.append(f'<div class="sys">↯ caller interrupted · heard: "{e(r.get("heard"))}"</div>')
        elif kind == 'turn':
            parts.append(f'<div class="sys">first word {e(r.get("first_word_ms"))} ms · turn {e(r.get("total_ms"))} ms</div>')
        elif kind == 'finish':
            parts.append(f'<div class="sys">call ended by agent: {e(r.get("reason"))}</div>')
        elif kind == 'suppressed':
            parts.append(f'<div class="sys">muted (question already asked): "{e(r.get("text"))}"</div>')
        elif kind == 'error':
            parts.append(f'<div class="report">Error: {e(r.get("text"))}</div>')
    review_html = ('<p class="muted">Not reviewed yet.</p>' if reviewed is None else
                   '<p><span class="pill ok">✓ No issues found</span></p>' if not reviewed['issues'] else
                   '<ul>' + ''.join(f'<li>{e(i)}</li>' for i in reviewed['issues']) + '</ul>')
    made_html = f'<div class="notice">Added scenario <b>{e(made)}</b> to scenarios.json (tagged draft). Edit its expectations, then run it from Tests.</div>' if made else ''
    body = f"""<p><a href="/calls">← Calls</a></p><h1>Call {e(info['reference'] or info['file'])}</h1>
    <p class="muted">{when(info['at'])} · caller …{e(info['caller'] or '???')} · {info['turns']} caller turns · ended {e(info['ended'])} · {call_status({**info, 'review': reviewed})}</p>
    {f'<p class="muted">Version {e(v.get("code"))} · {e(v.get("model"))} · effort {e(v.get("effort"))} · thinking {e(v.get("thinking"))} · prompt {e(v.get("prompt"))} · knowledge {e(v.get("knowledge"))}</p>' if v else '<p class="muted">Recorded before version stamps were added.</p>'}
    {made_html}<div class="grid2"><div class="card"><b>Automatic review</b>{review_html}
      <form class="inline" method="post" action="/calls/{e(info['file'])}/review">{token_field()}<button class="secondary">{'Review again' if reviewed else 'Review this call'} (≈1¢)</button></form></div>
    <div class="card"><b>Turn into a test</b><p class="muted">Adds a draft scenario that replays the caller's lines{', with the reported problem as a judge criterion' if info['reports'] else ''}.</p>
      {f'<form class="inline" method="post" action="/calls/{e(info["file"])}/scenario">{token_field()}<button>Make test scenario</button></form>' if (info['reference'] or info['caller']) else '<p class="muted">This call was recorded before call references, so it cannot be converted automatically.</p>'}</div></div>
    <h2>Transcript</h2><div class="chat">{''.join(parts)}</div>"""
    return page('Call', body, '/calls')


@app.post('/calls/{name}/scenario')
async def call_to_scenario(name: str, token: str = Form('')):
    check_token(token)
    path = safe_child(transcripts.FOLDER, name, r'[\w.-]+\.jsonl')
    info = transcripts.summary(path)
    key = info['reference'] or info['caller']
    if not key:
        raise HTTPException(400, 'This call has no reference or caller digits to look it up by.')
    try:
        scenario = harness.scenario_from_call(key)
        harness.add_scenario(scenario)
    except SystemExit as stop:
        raise HTTPException(409, str(stop))
    return RedirectResponse(f'/calls/{name}?made={scenario["id"]}', status_code=303)


@app.post('/calls/{name}/review')
async def call_review(name: str, token: str = Form('')):
    check_token(token)
    path = safe_child(transcripts.FOLDER, name, r'[\w.-]+\.jsonl')
    info = transcripts.summary(path)
    key = info['reference'] or info['caller']
    command = [sys.executable, 'review.py', '--budget', '0.05'] + (['--again', key] if key else [])
    return RedirectResponse(f'/jobs/{start_job(f"Review call {key or path.name}", command)}', status_code=303)


@app.get('/reports')
async def reports_page():
    reports = list(reversed(pilot_log.read_reports()))
    files = {}
    for path in transcripts._calls():
        m = re.search(r'-ref(\d{4})-', path.name)
        if m:
            files[m.group(1)] = path.name
    rows = ''.join(f"""<tr><td>{when(r['at'])}</td><td>{f'<a href="/calls/{e(files[r["reference"]])}">{e(r["reference"])}</a>' if r['reference'] in files else e(r['reference'])}</td>
        <td>…{e(r['caller'] or '???')}</td><td>{e(r['description'])}</td></tr>""" for r in reports)
    body = ('<h1>Problem reports</h1><p class="muted">Reported by testers during calls ("I want to report a problem"). '
            'Reports posted in Teams are not shown here: match them by reference on the Calls page.</p>'
            + (f'<table><tr><th>When</th><th>Reference</th><th>Caller</th><th>What they said</th></tr>{rows}</table>' if reports
               else '<p class="muted">No spoken reports yet.</p>'))
    return page('Reports', body, '/reports')


@app.get('/reviews')
async def reviews_page(file: str = ''):
    folder = review.FOLDER
    reports = sorted(folder.glob('2*.md'), reverse=True) if folder.is_dir() else []
    chosen = safe_child(folder, file, r'\d{8}-\d{6}\.md') if file else (reports[0] if reports else None)
    items = ''.join(f'<li><a href="/reviews?file={e(p.name)}">{e(p.stem[:8])} {e(p.stem[9:11])}:{e(p.stem[11:13])}</a></li>' for p in reports[:30])
    body = f"""<h1>Reviews</h1><p class="muted">The judge marks each new call against the agent's rules (about 1 cent per call, capped).</p>
    <form class="inline" method="post" action="/reviews/run">{token_field()}<button>Review new calls</button></form>
    <div style="display:grid;grid-template-columns:180px 1fr;gap:18px;margin-top:14px"><div><b>Past reviews</b><ul>{items or '<li class="muted">none yet</li>'}</ul></div>
    <div class="card">{markdown(chosen.read_text(encoding='utf-8')) if chosen else '<p class="muted">No review reports yet.</p>'}</div></div>"""
    return page('Reviews', body, '/reviews')


@app.post('/reviews/run')
async def reviews_run(token: str = Form('')):
    check_token(token)
    return RedirectResponse(f'/jobs/{start_job("Review new calls", [sys.executable, "review.py", "--budget", "0.20"])}', status_code=303)


@app.get('/tests')
async def tests_page():
    runs = harness_runs()
    scenarios = json.loads(harness.SCENARIOS.read_text(encoding='utf-8'))['scenarios']
    tags = sorted({t for s in scenarios for t in s.get('tags', [])})
    rows = ''.join(f"""<tr><td><a href="/tests/{e(r['name'])}">{e(r['data']['settings'].get('started'))}</a></td>
        <td><span class="pill {'ok' if r['passed'] == r['total'] and not r['data'].get('skipped') else 'warn'}">{r['passed']}/{r['total']}</span></td>
        <td>US${r['data']['cost']:.3f}</td><td>{f"{r['median'] / 1000:.1f} s" if r['median'] else '–'}</td>
        <td class="muted">{e(r['data']['settings'].get('model'))} · thinking {e(r['data']['settings'].get('thinking') or 'default')}</td></tr>""" for r in runs)
    body = f"""<h1>Tests</h1><p class="muted">The conversation harness runs scenarios through the same engine as the phone. Tickets are always dry-run.</p>
    <div class="card"><b>Run tests</b><form method="post" action="/tests/run" style="margin-top:8px">{token_field()}
      <select name="selection"><option value="all">All {len(scenarios)} scenarios</option>
        {''.join(f'<option value="tag:{e(t)}">Tag: {e(t)}</option>' for t in tags)}</select>
      <input name="only" placeholder="or IDs, e.g. HW-01,TIX-01" size="24">
      repeat <select name="repeat"><option>1</option><option>2</option><option>3</option></select>
      thinking <select name="thinking"><option value="">current setting</option><option value="between_tools">off</option><option value="adaptive">adaptive</option></select>
      budget US$ <select name="budget"><option>0.25</option><option selected>0.50</option><option>1.00</option></select>
      <button>Start</button></form></div>
    <h2>Past runs</h2>{f'<table><tr><th>Run</th><th>Passed</th><th>Cost</th><th>First word (median)</th><th>Settings</th></tr>{rows}</table>' if runs else '<p class="muted">No runs yet.</p>'}
    <h2>Scenarios</h2><table><tr><th>ID</th><th>Caller</th><th>Tags</th><th>Title</th></tr>
    {''.join(f"<tr><td>{e(s['id'])}</td><td>{'script' if 'script' in s['caller'] else 'simulated'}</td><td class='muted'>{e(', '.join(s.get('tags', [])))}</td><td>{e(s['title'])}</td></tr>" for s in scenarios)}</table>"""
    return page('Tests', body, '/tests')


@app.post('/tests/run')
async def tests_run(token: str = Form(''), selection: str = Form('all'), only: str = Form(''), repeat: str = Form('1'),
                    thinking: str = Form(''), budget: str = Form('0.50')):
    check_token(token)
    command = [sys.executable, 'harness.py', '--budget', str(min(float(budget), 1.0)), '--repeat', str(max(1, min(int(repeat), 3)))]
    if only.strip():
        if not re.fullmatch(r'[A-Za-z0-9,\- ]+', only):
            raise HTTPException(400, 'Scenario IDs: letters, digits, dashes and commas only.')
        command += ['--only', only.replace(' ', '')]
    elif selection.startswith('tag:'):
        command += ['--tag', selection[4:]]
    if thinking in ('between_tools', 'adaptive'):
        command += ['--set', f'thinking={thinking}']
    return RedirectResponse(f'/jobs/{start_job("Conversation tests", command)}', status_code=303)


@app.get('/tests/{name}')
async def test_run(name: str):
    folder = safe_child(harness.RUNS, name, r'\d{8}-\d{6}')
    data = json.loads((folder / 'results.json').read_text(encoding='utf-8'))
    blocks = []
    for r in data['results']:
        checks = ''.join(f"<li>{'✅' if c['passed'] else '❌'} {e(c['check'])}{' — ' + e(c['detail']) if c['detail'] and not c['passed'] else ''}</li>" for c in r['checks'])
        verdicts = ''.join(f"<li>{'✅' if v['passed'] else '❌'} judge: {e(v['criterion'])} <span class='muted'>({e(v['reason'])})</span></li>" for v in r['verdicts'])
        transcript = harness.render_transcript({**r['run'], 'transcript': [tuple(t) for t in r['run']['transcript']]})
        blocks.append(f"""<details {'open' if not r['passed'] else ''}><summary>{'✅' if r['passed'] else '❌'} {e(r['id'])}: {e(r['title'])}
          {f"(run {r['repeat']})" if r['repeat'] > 1 else ''} <span class="muted">· US${r['cost']:.3f}</span></summary>
          <ul>{checks}{verdicts}</ul><pre>{e(transcript)}</pre></details>""")
    s = data['settings']
    body = f"""<p><a href="/tests">← Tests</a></p><h1>Test run {e(s.get('started'))}</h1>
    <p class="muted">{sum(r['passed'] for r in data['results'])}/{len(data['results'])} passed · US${data['cost']:.3f} · {e(s.get('model'))} effort {e(s.get('effort'))}
    thinking {e(s.get('thinking') or 'default')} · judge {e(s.get('judge_model'))}{' · not run (budget): ' + e(', '.join(data['skipped'])) if data.get('skipped') else ''}</p>
    {''.join(blocks)}"""
    return page('Test run', body, '/tests')


@app.get('/jobs/{job_id}')
async def job_page(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404)
    body = f"""<h1>{e(job['title'])}</h1><p class="muted" id="state">{'Finished' if job['done'] else 'Running…'}</p>
    <div class="log" id="log">{e(chr(10).join(job['lines']))}</div><p id="links"></p>
    <script>
    async function poll(){{
      const r = await fetch('/jobs/{job_id}/status'); const j = await r.json();
      document.getElementById('log').textContent = j.lines.join('\\n');
      document.getElementById('state').textContent = j.done ? ('Finished' + (j.code ? ' (some checks failed or the budget stopped it)' : '')) : 'Running…';
      if (j.done) {{ document.getElementById('links').innerHTML = j.link ? '<a class="btn" href="' + j.link + '">Open results</a>' : ''; }}
      else setTimeout(poll, 1500);
    }}
    poll();
    </script>"""
    return page(job['title'], body, '/tests' if 'test' in job['title'].lower() else '/reviews')


@app.get('/jobs/{job_id}/status')
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404)
    link = None
    if job['done']:
        text = '\n'.join(job['lines'])
        m = re.search(r'harness[\\/](\d{8}-\d{6})[\\/]report\.md', text)
        r = re.search(r'reviews[\\/](\d{8}-\d{6}\.md)', text)
        link = f'/tests/{m.group(1)}' if m else (f'/reviews?file={r.group(1)}' if r else None)
    return JSONResponse({'lines': job['lines'][-400:], 'done': job['done'], 'code': job['code'], 'link': link})


@app.get('/settings')
async def settings_page(saved: str = ''):
    cfg = read_config()
    descriptions = {
        'anthropic_model': 'Claude model for replies', 'effort': 'How hard the model works on each reply',
        'thinking': 'between_tools = off (fastest); adaptive = may think before replying',
        'tickets': 'live = real Zoho tickets; dry_run = saved locally; off = no ticket tool',
        'transcripts': 'Save and show call transcripts (staff pilot only)', 'call_reference': 'Speak a call reference at the end',
        'staff_feedback': 'Let callers report a problem by voice', 'flows': 'on = tickets only through the ticket flow; off = direct ticket tool', 'say_voice': 'Voice for the fixed final words',
        'ticket_subject_prefix': 'Added to every ticket subject', 'voice': 'ElevenLabs voice ID for the agent',
        'ttsProvider': 'Speech provider', 'language': 'Speech language', 'hints': 'Words the speech recogniser should expect'}
    rows = []
    for key, allowed in configure.SETTINGS.items():
        current = cfg.get(key, '')
        if allowed:
            field = (f'<select name="value">' + ''.join(f'<option {"selected" if o == current else ""}>{e(o)}</option>' for o in sorted(allowed))
                     + (f'<option selected value="">(default)</option>' if current == '' else '') + '</select>')
        else:
            field = f'<input name="value" value="{e(current)}" size="40" placeholder="(default)">'
        rows.append(f"""<tr><td><b>{e(key)}</b><div class="muted">{e(descriptions.get(key, ''))}</div></td>
          <td><form method="post" action="/settings">{token_field()}<input type="hidden" name="key" value="{e(key)}">{field} <button>Save</button></form></td></tr>""")
    notice = f'<div class="notice">Saved <b>{e(saved)}</b>. It applies from the next call (voice settings) or the next reply (everything else). No restart needed.</div>' if saved else ''
    body = f"""<h1>Settings</h1><p class="muted">Non-secret settings from the private config. Credentials are never shown here; change them with <code>configure.py</code>.</p>
    {notice}<table><tr><th style="width:45%">Setting</th><th>Value</th></tr>{''.join(rows)}</table>"""
    return page('Settings', body, '/settings')


@app.post('/settings')
async def settings_save(token: str = Form(''), key: str = Form(...), value: str = Form('')):
    check_token(token)
    if key not in configure.SETTINGS:
        raise HTTPException(400, 'Unknown setting')
    cfg = read_config()
    if not cfg:
        raise HTTPException(400, 'No private configuration yet: run configure.py first.')
    if value.strip() == '':
        cfg.pop(key, None)
    else:
        try:
            configure.apply_setting(cfg, f'{key}={value}')
        except ValueError as error:
            raise HTTPException(400, str(error))
    if cfg.get('provider') == 'anthropic':
        import claude_reply
        try:
            claude_reply.request_options(cfg)
        except ValueError as error:
            raise HTTPException(400, f'Not saved: {error}')
    configure.save_config(cfg)
    return RedirectResponse(f'/settings?saved={key}', status_code=303)


@app.get('/flows')
async def flows_page():
    try:
        defs = flow_engine.load_flows()
    except flow_engine.FlowError as error:
        return page('Flows', f'<h1>Flows</h1><p><span class="pill bad">Flow files are invalid: {e(error)}</span></p>', '/flows')
    cfg = read_config()
    state = ('<span class="pill ok">on</span>' if cfg.get('flows', 'on') != 'off'
             else '<span class="pill warn">off: tickets use the direct tool</span>')
    blocks = []
    for flow in defs.values():
        fields = flow['fields']
        steps = [f'<div class="fstep end"><b>{e(flow["title"])}</b><div class="muted">starts when: {e(flow["use_when"])}</div></div>']
        for number, step in enumerate(flow['steps'], 1):
            if step['kind'] == 'collect':
                need = [fields[f]['label'] for f in step.get('fields', [])]
                if step.get('one_of'):
                    need.append(' or '.join(fields[f]['label'] for f in step['one_of']))
                extra = [fields[f]['label'] for f in step.get('also', [])]
                detail = ('Needs: ' + ', '.join(need)) + ((' · optional: ' + ', '.join(extra)) if extra else '')
                kind = 'collect · each value checked in code'
            elif step['kind'] == 'confirm':
                detail = 'System writes the read-back; the caller must say yes in a later turn. "No" → correct a detail → new read-back.'
                kind = 'confirm'
            else:
                detail = f'System runs {step["action"]} only after the yes. The AI cannot run it directly.'
                kind = 'action'
            steps.append('<div class="arrow">↓</div>')
            steps.append(f'<div class="fstep {step["kind"]}"><div class="kind">{number} · {e(kind)}</div><b>{e(step["title"])}</b>'
                         f'<div class="muted">{e(detail)}</div><div style="margin-top:4px">{e(step["instruction"])}</div></div>')
        steps.append('<div class="arrow">↓</div>')
        steps.append(f'<div class="fstep end"><div class="muted">Success: {e(flow["on_success"])}<br>Failure: {e(flow["on_failure"])}</div></div>')
        field_rows = ''.join(f"<tr><td>{e(spec['label'])}</td><td><code>{e(name)}</code></td><td>{e(spec['type'])}</td>"
                             f"<td>{'optional' if spec.get('optional') else 'required'}</td><td class='muted'>{e(spec.get('ask', ''))}</td></tr>"
                             for name, spec in fields.items())
        blocks.append(f"""<h2>{e(flow['title'])} <span class="muted">({e(flow['id'])})</span></h2>
          <div class="grid2"><div class="flow">{''.join(steps)}</div>
          <div><table><tr><th>Field</th><th>Name</th><th>Type</th><th></th><th>Asked as</th></tr>{field_rows}</table>
          <details><summary>Mermaid diagram (for documents)</summary><pre>{e(flow_engine.mermaid(flow))}</pre></details></div></div>""")
    body = (f'<h1>Flows</h1><p>Flows are {state}. Each flow is a file in <code>voice-agent/flows/</code>; this page is drawn from those files. '
            'The order is enforced in code: a step\'s tools are refused until the earlier steps are done.</p>' + ''.join(blocks))
    return page('Flows', body, '/flows')


@app.get('/knowledge')
async def knowledge_page():
    registry = json.loads((LIBRARY / 'registry.json').read_text(encoding='utf-8'))
    try:
        core = len(Library().system_text())
        status = f'<span class="pill ok">All pilot sources verified</span> core text {core:,} of {CORE_LIMIT:,} characters'
    except Exception as error:
        status = f'<span class="pill bad">Calls would be refused: {e(error)}</span>'
    rows = []
    for item in registry['entries']:
        path = LIBRARY / item['path']
        ok = path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == item.get('content_sha256')
        mode = DELIVERY.get(item['id'])
        rows.append(f"""<tr class="{'' if mode else 'muted'}"><td><b>{e(item['id'])}</b><div class="muted">{e(item.get('name', ''))}</div></td>
          <td>{e(item['version'])}</td><td>{f'<span class="pill info">{e(mode)}</span>' if mode else '<span class="pill grey">not used</span>'}</td>
          <td>{e(item['readiness'])}</td><td>{'<span class="pill ok">matches</span>' if ok else '<span class="pill bad">changed</span>'}</td>
          <td>{path.stat().st_size if path.exists() else 0:,}</td><td class="muted">{e(item.get('description', ''))[:160]}</td></tr>""")
    body = f"""<h1>Knowledge</h1><p>{status}</p><p class="muted">core = in every request · guide = fetched by ID once the screen is identified ·
    search = searched when needed. To change a document: edit it, update its registry entry (version, fingerprint, review), run the tests, restart.</p>
    <table><tr><th>Source</th><th>Version</th><th>Delivery</th><th>Readiness</th><th>Fingerprint</th><th>Bytes</th><th>Use when</th></tr>{''.join(rows)}</table>"""
    return page('Knowledge', body, '/knowledge')


def main():
    import uvicorn
    global PORT, ALLOWED_HOSTS
    if '--port' in sys.argv:  # e.g. run a branch's dashboard next to the main one: --port 8103
        PORT = int(sys.argv[sys.argv.index('--port') + 1])
        ALLOWED_HOSTS = {f'{HOST}:{PORT}', f'localhost:{PORT}'}
    if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    print(f'Dashboard: http://{HOST}:{PORT}  (this PC only; Ctrl+C to stop)', flush=True)
    if '--no-browser' not in sys.argv:
        webbrowser.open(f'http://{HOST}:{PORT}')
    uvicorn.run(app, host=HOST, port=PORT, log_level='warning')


if __name__ == '__main__':
    main()
