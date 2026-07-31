const $ = id => document.getElementById(id);

// Actions carrying DATA are wired through data-* attributes and this one
// delegated listener, never an inline onclick. HTML-escaping is not enough
// inside a JS string inside an attribute: the parser decodes &#39; back to a
// quote BEFORE the JS parser runs, so an id containing one would break out
// and execute. dataset hands the decoded text straight to a variable, where
// it is data and stays data.
document.addEventListener('click', ev => {
 const el = ev.target.closest('[data-act]');
 if(!el) return;
 const d = el.dataset;
 if(d.act === 'queue')  return queueOne(d.rk, +d.level);
 if(d.act === 'series') return queueSeries(d.sid, +d.level, d.season);
 if(d.act === 'share')  return hubUpload(d.cid);
 if(d.act === 'bundle') return exportBundle(el);
 if(d.act === 'pick')   return togglePick(d.cid, d.pass);
 if(d.act === 'deepen') return deepenRow(d.rk, d.cid, d.label);
 if(d.act === 'log')    return showLog(+d.id);
 if(d.act === 'label')  return openLabelling(d.cid);
 // Repaint straight away rather than waiting for the next poll: a fold that
 // takes two seconds to respond reads as a dead control.
 if(d.act === 'toglog') { logOpen = !logOpen; return poll(); }
 if(d.act === 'stop')   return stopJob(+d.id);
});
const j = r => r.json();
const post = (u, b) => fetch(u, {method:'POST',
  headers:{'content-type':'application/json'},
  body: b ? JSON.stringify(b) : undefined});
// Library titles are third-party strings; never interpolate them raw.
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const plural = (n, w) => n + ' ' + w + (n === 1 ? '' : 's');

function hhmm(sec){
 if(sec < 90) return Math.max(1, Math.round(sec)) + 's';
 if(sec < 5400) return Math.round(sec / 60) + 'm';
 return (sec / 3600).toFixed(sec < 36000 ? 1 : 0) + 'h';
}
function span(range){
 const [lo, hi] = range;
 return lo === hi ? hhmm(lo) : hhmm(lo) + '–' + hhmm(hi);
}
function money(range){
 const [lo, hi] = range;
 if(!hi) return 'free';
 return '$' + lo.toFixed(2) + '–' + hi.toFixed(2);
}

let SETUP = null, LEVEL = 0, LIB = '', PLAN = null, PLANNING = false;
// Music is the only paid step. It rides on the full index but is separable:
// unchecking it sends skip=music, giving a video-only run even with a token.
let MUSIC = true;

// ---- setup -----------------------------------------------------------------

// Whether setup is being shown ON PURPOSE by someone already past it. Before
// this, `ready` both meant "setup is complete" and "setup is unreachable", so
// there was no way back to add an optional key later.
let SETUP_OPEN = false;

async function loadSetup(){
 SETUP = await j(await fetch('api/setup'));
 const on = SETUP.ready && !SETUP_OPEN;
 $('setupView').hidden = on;
 $('runView').hidden = !on;
 $('storeView').hidden = !on;
 $('setupToggle').hidden = !SETUP.ready;
 $('setupToggle').textContent = SETUP_OPEN ? 'done' : 'setup';
 $('setupToggle').onclick = () => { SETUP_OPEN = !SETUP_OPEN; loadSetup(); };
 if(on){ renderRun(); loadStore(); } else renderSetup();
}

function renderSetup(){
 const s = SETUP, server = s.backend === 'jellyfin' ? s.jellyfin : s.plex;
 const connected = !!server.origin;
 const steps = [];

 steps.push(step(connected ? 'done' : 'now', connected ? '✓' : '1',
  'Media server', connected ? '' : 'needed',
  connected
   ? esc(s.backend) + ' · <span class="mono">' + esc(server.origin) + '</span>'
   : 'Sign-in happens on plex.tv. This app gets a token you can revoke, not '
     + 'your password.',
  connected
   ? '<button class="link sm" onclick="serverUI(1)">change</button>'
   : '<div id="serverUI"></div>', !connected));

 steps.push(step(s.tmdbConfigured ? 'done' : (connected ? 'now' : 'opt'),
  s.tmdbConfigured ? '✓' : '2', 'TMDb key',
  s.tmdbConfigured ? '' : 'needed',
  'Free from themoviedb.org. Titles are identified by TMDb id, so nothing '
  + 'runs without one.',
  s.tmdbConfigured ? '' :
   '<div class="row"><input id="tmdbKey" style="flex:1" placeholder="paste key">'
   + '<button onclick="saveKeys()">Save</button></div>'));

 steps.push(step('opt', '+', 'AudD token', 'optional',
  'Names the songs during a full index. Billed per music cue, about $0.005 '
  + 'each. Without it, music is skipped.',
  '<div class="row"><input id="auddKey" style="flex:1" placeholder="AudD token'
  + (s.auddConfigured ? ' (set)' : '') + '">'
  + '<button class="ghost" onclick="saveKeys()">Save</button></div>'));

 // Animated titles only, so it sits below the two required steps and the
 // state is fetched separately: it means asking the engine container, and
 // /api/setup is polled often enough that a per-poll round trip to a
 // container most people never start would be a waste.
 steps.push(step('opt', '+', 'Speakers', 'animation',
  'Animated titles have no faces to detect, so who-speaks-when comes from the '
  + 'audio instead. Needs a one-time model download.',
  '<div id="spkUI" class="spkbox">checking…</div>'));

 // No input here on purpose: the hub is configured, not chosen. Pointing
 // the stack elsewhere is a dev concern and stays on XRAY_HUB_URL.
 steps.push(step(s.hubUrl ? 'done' : 'opt', s.hubUrl ? '✓' : '+',
  'Hub', s.hubUrl ? '' : 'off',
  s.hubUrl
   ? 'Sharing through <span class="mono">' + esc(s.hubUrl) + '</span>. '
     + 'A full index checks it first and downloads a title when someone has '
     + 'already done that one, instead of computing it again.'
   : 'Turned off, so every title is computed locally and nothing is shared.',
  ''));

 $('setupView').innerHTML =
  '<div><h2>Setup</h2><p class="sub">The first two are required.</p></div>'
  + steps.join('');
 if(!connected) serverUI(0);
 speakersUI();
}

// ---- speaker model weights -------------------------------------------------
//
// The pyannote weights are openly licensed but gated behind an accept-the-
// conditions click, so this step exists to make that click findable. It is the
// one thing a UI can do that a failed `docker build` cannot: say WHICH gate is
// missing, and link it.

async function speakersUI(){
 const box = $('spkUI');
 if(!box) return;
 let s;
 try { s = await j(await fetch('api/speakers/models')); }
 catch(e){ box.innerHTML = 'could not reach the orchestrator'; return; }
 box.innerHTML = spkBody(s);
 spkSync(s);
}

// Keep SETUP's copy of the state in step, so the store table starts offering
// the pass the moment the weights land. Assigning rather than re-fetching
// /api/setup: the answer is already in hand, and a re-render here would
// discard the message the user is reading.
function spkSync(s){
 if(SETUP) SETUP.speakers = {available: s.state === 'ready', state: s.state};
}

function spkBody(s){
 const field = '<div class="row"><input id="hfKey" style="flex:1" '
  + 'placeholder="HuggingFace token' + (s.tokenSet ? ' (set)' : '') + '">'
  + '<button class="ghost" onclick="spkSave()">Save &amp; download</button>'
  + '</div>';

 if(s.state === 'off' || s.state === 'unreachable')
  // Its own sentence rather than s.message: that string ends in the command,
  // which is right for a job log and a duplicate here, where the command gets
  // its own copyable box. .cmd, not .code -- .code is the Jellyfin pairing-code
  // style, 26px bold with 6px of letter-spacing: right for six characters,
  // absurd for a shell command.
  return 'The speakers container is not running. It is opt-in, because it '
   + 'carries ~200 MB of torch that live-action titles never touch.'
   + '<div class="cmd">docker compose --profile speakers up -d</div>';

 if(s.state === 'ready')
  return '<b>Ready.</b> ' + (s.baked
   ? 'The weights are baked into the image, so no token is needed here.'
   : 'Weights downloaded — animated titles can be indexed.');

 if(s.state === 'fetching')
  return 'Downloading the weights (about 100 MB). This page can be left open.';

 if(s.state === 'gated')
  // The whole point of doing this in a UI: name the gate and link it.
  return 'The token works, but these conditions are not accepted yet — open '
   + 'each one, click Agree, then download again:<ul>'
   + (s.gated || []).map(r => '<li><a target="_blank" rel="noopener noreferrer" href="'
      + esc((s.hfUrl || 'https://huggingface.co/') + r) + '">'
      + esc(r) + '</a></li>').join('')
   + '</ul><button class="ghost" onclick="spkFetch()">Download again</button>';

 if(s.state === 'bad-token')
  return 'HuggingFace rejected that token. Create a <b>read</b> token at '
   + '<a target="_blank" rel="noopener noreferrer" href="https://huggingface.co/settings/tokens">'
   + 'huggingface.co/settings/tokens</a>.' + field;

 if(s.state === 'needs-fetch')
  return 'A token is saved but the weights are not downloaded yet.'
   + '<div class="row"><button onclick="spkFetch()">Download weights</button>'
   + '</div>';

 if(s.state === 'load-failed')
  return 'The download did not finish: <span class="mono">'
   + esc((s.message || '').slice(-300)) + '</span>'
   + '<div class="row"><button class="ghost" onclick="spkFetch()">Try again'
   + '</button></div>';

 // no-token, and anything a future engine version invents
 return 'Needs a free HuggingFace token once, to fetch the models. Accept the '
  + 'conditions on <a target="_blank" rel="noopener noreferrer" href="https://huggingface.co/pyannote/'
  + 'speaker-diarization-3.1">speaker-diarization-3.1</a> (and the two repos '
  + 'it pulls), then paste a read token here. It is used for the download and '
  + 'nothing else.' + field;
}

async function spkSave(){
 const el = $('hfKey');
 if(el && el.value.trim()){
  await fetch('api/settings', {method:'PUT',
   headers:{'content-type':'application/json'},
   body: JSON.stringify({hf_token: el.value.trim()})});
 }
 return spkFetch();
}

async function spkFetch(){
 const box = $('spkUI');
 box.innerHTML = 'Downloading the weights (about 100 MB) — this takes a few '
  + 'minutes and only happens once…';
 // Any failure re-reads the state instead of reporting one. The engine keeps
 // the verdict for its last attempt, so a GET gives the specific answer
 // ("segmentation-3.0 is not accepted") where this response only has an HTTP
 // status -- and a long request cut by a proxy is not a failed download.
 try {
  const r = await post('api/speakers/models');
  if(!r.ok) return speakersUI();
  const s = Object.assign({tokenSet: true}, await j(r));
  box.innerHTML = spkBody(s);
  spkSync(s);
 } catch(e){ speakersUI(); }
}

function step(state, bullet, title, tag, body, extra, openNow){
 return '<div class="step ' + state + '"><div class="bul">' + bullet + '</div>'
  + '<div class="body"><div class="spread"><b>' + title + '</b>'
  + (tag ? '<span class="tag ' + (state === 'opt' ? 'n' : 'g') + '">' + tag
           + '</span>' : '')
  + '</div><p>' + body + '</p>' + (extra || '') + '</div></div>';
}

function serverUI(force){
 const el = $('serverUI') || $('setupView');
 const box = $('serverUI');
 if(!box){ renderSetup(); return; }
 box.innerHTML =
  '<div class="row"><button onclick="plexSignIn()">Sign in with Plex</button>'
  + '<span class="meta">or Jellyfin:</span>'
  + '<input id="jfOrigin" placeholder="http://jellyfin:8096" style="flex:1">'
  + '<button class="ghost" onclick="jfQuick()">Quick Connect</button></div>'
  + '<div class="row"><input id="jfUser" placeholder="user">'
  + '<input id="jfPass" type="password" placeholder="password">'
  + '<button class="ghost" onclick="jfPassword()">password sign-in</button></div>'
  + '<div id="flow"></div>';
}

async function plexSignIn(){
 const pin = await j(await post('api/auth/plex/pin'));
 window.open(pin.authUrl, '_blank');
 $('flow').innerHTML = '<div class="card meta">Finish signing in on the Plex '
  + 'tab… <span class="mono">' + esc(pin.code.slice(0, 4)) + '…</span></div>';
 for(let i = 0; i < 120; i++){
  await new Promise(r => setTimeout(r, 3000));
  if((await j(await fetch('api/auth/plex/pin/' + pin.id))).claimed) return pickServer();
 }
}

async function pickServer(){
 const data = await j(await fetch('api/auth/plex/servers'));
 const opts = [];
 for(const srv of data.servers)
  for(const c of srv.connections)
   opts.push('<option value="' + esc(c.uri) + '">' + esc(srv.name) + ' · '
    + esc(c.uri) + (c.local ? ' (local)' : '') + (c.relay ? ' (relay)' : '')
    + '</option>');
 $('flow').innerHTML = '<div class="card row"><select id="srvPick" style="flex:1">'
  + opts.join('') + '</select><button onclick="saveServer()">Use this server</button></div>';
}

async function saveServer(){
 const r = await j(await post('api/auth/plex/origin', {uri: $('srvPick').value}));
 $('flow').innerHTML = r.reachable ? '' : '<div class="card warn">Saved, but this '
  + 'address is not reachable from the container: pick a different connection '
  + '(a LAN address can be dead from here while the remote one works).</div>';
 loadSetup();
}

async function jfQuick(){
 const origin = $('jfOrigin').value.trim();
 if(!origin) return;
 const r = await j(await post('api/auth/jellyfin/quickconnect', {origin}));
 if(!r.enabled){ $('flow').innerHTML = '<div class="card warn">Quick Connect is '
  + 'disabled on this server; use password sign-in.</div>'; return; }
 $('flow').innerHTML = '<div class="card">Enter this code in Jellyfin '
  + '(Settings → Quick Connect):<div class="code">' + esc(r.code) + '</div></div>';
 for(let i = 0; i < 120; i++){
  await new Promise(res => setTimeout(res, 3000));
  if((await j(await fetch('api/auth/jellyfin/quickconnect/' + r.secret))).claimed){
   $('flow').innerHTML = ''; return loadSetup();
  }
 }
}

async function jfPassword(){
 const r = await post('api/auth/jellyfin/password', {
  origin: $('jfOrigin').value.trim(), username: $('jfUser').value,
  password: $('jfPass').value});
 if(r.ok) loadSetup();
 else $('flow').innerHTML = '<div class="card warn">'
  + esc((await r.json()).detail || 'sign-in failed') + '</div>';
}

async function saveKeys(){
 const body = {};
 for(const [id, field] of [['tmdbKey','tmdb_key'], ['auddKey','audd_token']]){
  const el = $(id);
  if(el && el.value.trim()) body[field] = el.value.trim();
 }
 if(!Object.keys(body).length) return;
 await fetch('api/settings', {method:'PUT',
  headers:{'content-type':'application/json'}, body: JSON.stringify(body)});
 loadSetup();
}

// ---- run composer ----------------------------------------------------------

async function renderRun(){
 const libs = await j(await fetch('api/libraries')).catch(() => ({sections:[]}));
 const opts = (libs.sections || []).map(s =>
  '<option value="' + esc(s.title) + '"' + (s.title === LIB ? ' selected' : '')
  + '>' + esc(s.title) + '</option>').join('');
 $('runView').innerHTML =
  '<div class="row"><input id="q" style="flex:1" placeholder="Search a title…"'
  + ' onkeydown="if(event.key===\'Enter\')doSearch()">'
  + '<button class="ghost" onclick="doSearch()">Search</button></div>'
  + '<div id="results"></div>'
  + '<div class="card sec"><div class="row"><b>Whole library</b>'
  + '<select id="lib" onchange="pickLib()" style="flex:1">'
  + '<option value="">choose…</option>' + opts + '</select></div>'
  + '<div id="plan"></div></div>';
 if(LIB) pickLib();
}

async function pickLib(){
 LIB = $('lib').value;
 PLAN = null;
 if(!LIB){ $('plan').innerHTML = ''; return; }
 $('plan').innerHTML = '<p class="sub">Checking what you already have…</p>';
 PLANNING = true;
 const r = await fetch('api/plan?library=' + encodeURIComponent(LIB));
 PLANNING = false;
 if(!r.ok){
  $('plan').innerHTML = '<p class="sub warn">'
   + esc((await r.json()).detail) + '</p>';
  return;
 }
 PLAN = await r.json();
 renderPlan();
}

function renderPlan(){
 const p = PLAN, n = p.distinct || 1;
 const pct = x => (100 * x / n).toFixed(2) + '%';
 const lv = p.levels[String(LEVEL)];
 const hubBit = p.hubChecked
  ? (p.levels['1'].fromHub
     ? plural(p.levels['1'].fromHub, 'title') + ' already on the hub'
     : 'no hub coverage for these')
  : (SETUP.hubUrl ? 'hub unreachable' : 'no hub configured');

 $('plan').innerHTML =
  '<div class="spread"><h2>' + esc(p.library) + '</h2>'
  + '<span class="mono">' + plural(p.total, 'title') + '</span></div>'
  + '<div class="cov" style="margin:.6rem 0 .5rem">'
  + '<span class="f" style="width:' + pct(p.haveFull) + '"></span>'
  + '<span class="s" style="width:' + pct(p.haveSeed) + '"></span>'
  + '<span class="h" style="width:' + pct(p.levels['1'].fromHub) + '"></span>'
  + '</div>'
  + '<div class="key">'
  + keyItem('var(--accent)', p.haveFull, 'fully indexed')
  + keyItem('var(--accent);opacity:.45', p.haveSeed, 'seeded')
  + keyItem('var(--ok);opacity:.6', p.levels['1'].fromHub, 'on the hub')
  + keyItem('var(--soft)', p.levels['1'].todo, 'not indexed')
  + '</div>'
  + (p.unidentified ? '<p class="sub">' + plural(p.unidentified, 'title')
     + ' have no TMDb match and will be skipped.</p>' : '')
  + '<div class="tiers" style="margin-top:.9rem">'
  + tier(0, 'Quick seed', 'Cast, biographies and trivia. Does not read the '
      + 'video file.', p.levels['0'])
  + tier(1, 'Full index', 'Adds per-actor on-screen intervals'
      + (SETUP.auddConfigured ? ' and song names' : '')
      + '. Reads each video file.', p.levels['1'])
  + '</div>'
  + musicRow(p.levels['1'])
  + '<div class="spread" style="margin-top:.9rem">'
  + '<span class="meta">' + esc(hubBit)
  + (LEVEL === 0 ? '. Seeds can be deepened later without redoing this work.'
     : '') + '</span>'
  + '<button onclick="queueLibrary()"' + (lv.todo ? '' : ' disabled') + '>'
  + (lv.todo ? (LEVEL ? 'Index ' : 'Seed ') + plural(lv.todo, 'title')
             : 'Nothing to do') + '</button></div>'
  // Phrased without a subject-verb agreement to get right for any count.
  + (LEVEL === 1 && p.levels['1'].hubCouldServe
     ? '<p class="sub">The hub has full timelines for '
       + plural(p.levels['1'].hubCouldServe, 'seeded title')
       + ', but seeds are upgraded locally, so they count as work.</p>' : '');
}

function keyItem(color, n, label){
 return '<span><i style="background:' + color + '"></i>' + n + ' ' + label + '</span>';
}

function tier(level, title, body, lv){
 // Level 1 only costs money while music is on; level 0 never does.
 const cash = (level === 1 && MUSIC) ? lv.dollars : [0, 0];
 return '<button class="tier' + (LEVEL === level ? ' on' : '') + '"'
  + ' onclick="setLevel(' + level + ')"><div class="spread"><b>' + title + '</b>'
  + '<span class="radio"></span></div><p>' + body + '</p>'
  + '<div class="cost"><span>' + (lv.todo ? span(lv.seconds) : '—')
  + ' <i>total</i></span><span>' + money(cash) + '</span></div></button>';
}

function musicRow(lv){
 if(LEVEL !== 1 || !PLAN.auddAvailable || !lv.todo) return '';
 // The cap is a local spend guard, not an AudD tier: AudD's 300 free
 // requests are one-time at signup, so say whose limit this is.
 const cap = MUSIC && lv.titlesBeforeCap !== null
  ? '<p class="sub warn">Your spend cap (' + PLAN.auddHeadroom
    + ' calls left) stops music after about '
    + plural(lv.titlesBeforeCap, 'title') + '. Raise XRAY_AUDD_BUDGET to '
    + 'change it.</p>'
  : '';
 return '<label class="row" style="margin-top:.7rem;font-size:13px">'
  + '<input type="checkbox"' + (MUSIC ? ' checked' : '')
  + ' onchange="setMusic(this.checked)"> Name songs'
  + (MUSIC ? '<span class="meta">' + lv.cues[0] + '–' + lv.cues[1]
             + ' cues, $0.005 each</span>' : '')
  + '</label>' + cap;
}

function setMusic(on){ MUSIC = on; if(PLAN) renderPlan(); }
function setLevel(l){ LEVEL = l; if(PLAN) renderPlan(); }

// A full index with music off is a video-only run: the pipeline's own skip
// list, not a second code path.
// Passes that must never run unless explicitly ticked. `music` bills money;
// `speakers` costs an audio pull plus ~25 min of CPU and only makes sense for
// animated titles, where faces cannot work. Everything else is free and fast,
// so "Deepen" filling every gap stays the sensible default.
const OPT_IN = ['speakers'];

// Whether to offer the speakers pass at all. The container is opt-in and its
// weights are a separate download, so on most installs this is off and the
// honest thing is to say nothing rather than offer a button that fails.
// Setup → Speakers is where it gets turned on, and that step is always shown.
const speakersOffered = () =>
  !!(SETUP && SETUP.speakers && SETUP.speakers.available);

function runSkip(level){
 const off = OPT_IN.slice();
 if(level === 1 && !MUSIC) off.push('music');
 return off.join(',');
}

async function queueLibrary(){
 const r = await post('api/run',
  {library: LIB, level: LEVEL, skip: runSkip(LEVEL)});
 if(!r.ok) return alert((await r.json()).detail);
 poll();
}

async function doSearch(){
 const q = $('q').value.trim();
 if(!q) return;
 $('results').innerHTML = '<p class="sub">searching…</p>';
 const r = await fetch('api/search?q=' + encodeURIComponent(q));
 if(!r.ok){ $('results').innerHTML = '<p class="sub warn">'
  + esc((await r.json()).detail) + '</p>'; return; }
 const data = await r.json();
 if(!data.results.length){
  $('results').innerHTML = '<p class="sub">no matches in the library</p>'; return; }
 $('results').innerHTML = '<div class="card">'
  + data.results.map(resultRow).join('') + '</div>';
}

function resultRow(x){
 const rk = 'data-rk="' + esc(x.ratingKey) + '"';
 // A show is not playable, so it gets series-targeted buttons rather than the
 // per-item ones: there is no single file behind it to index.
 if(x.type === 'show'){
  const sid = 'data-sid="' + esc(x.seriesId) + '"';
  return '<div class="spread" style="padding:.25rem 0"><span>' + esc(x.label)
   + (x.year ? ' <span class="meta">(' + esc(x.year) + ')</span>' : '')
   + '</span><span class="row"><span class="mono">show</span>'
   + '<button class="ghost sm" data-act="series" ' + sid
   + ' data-level="0">Seed all</button>'
   + '<button class="sm" data-act="series" ' + sid
   + ' data-level="1">Full index all</button></span></div>';
 }
 const row = '<div class="spread" style="padding:.25rem 0"><span>' + esc(x.label)
  + (x.year ? ' <span class="meta">(' + esc(x.year) + ')</span>' : '')
  + '</span><span class="row"><span class="mono">' + esc(x.type) + '</span>'
  + '<button class="ghost sm" data-act="queue" ' + rk + ' data-level="0">Seed</button>'
  + '<button class="sm" data-act="queue" ' + rk + ' data-level="1">Full index</button>'
  + '</span></div>';
 // An episode brings its whole show with it, so the bulk options sit BELOW
 // the episode's own row instead of replacing it. Doing a whole series is the
 // common case; doing one episode is still a case, and dropping `row` here
 // left no way to reach it at all.
 if(!x.seriesId) return row;
 const sid = 'data-sid="' + esc(x.seriesId) + '"';
 const show = esc(x.series || 'this show');
 let bulk = '<div class="meta" style="padding:0 0 .35rem">'
  + 'All of ' + show + ': '
  + '<button class="link sm" data-act="series" ' + sid + ' data-level="0">seed</button>'
  + ' · <button class="link sm" data-act="series" ' + sid
  + ' data-level="1">full index</button>';
 // Season 0 is Specials — a real season — so test for a number, not truthiness.
 if(x.season !== null && x.season !== undefined && x.season !== ''){
  const sn = Number(x.season);
  const ssn = ' data-season="' + sn + '"';
  bulk += '<br>Season ' + sn + ' only: '
   + '<button class="link sm" data-act="series" ' + sid + ssn + ' data-level="0">seed</button>'
   + ' · <button class="link sm" data-act="series" ' + sid + ssn
   + ' data-level="1">full index</button>';
 }
 return row + bulk + '</div>';
}

async function queueSeries(seriesId, level, season){
 // `season` arrives from a data attribute, so it is a string or undefined.
 // Season 0 is Specials: only undefined/'' means "the whole show".
 const body = {series: seriesId, level, skip: runSkip(level)};
 if(season !== undefined && season !== '') body.season = Number(season);
 const r = await post('api/run', body);
 if(!r.ok) return alert((await r.json()).detail);
 $('results').innerHTML = ''; $('q').value = '';
 poll();
}

async function queueOne(ratingKey, level){
 const r = await post('api/run',
  {rating_key: ratingKey, level, skip: runSkip(level)});
 if(!r.ok) return alert((await r.json()).detail);
 $('results').innerHTML = ''; $('q').value = '';
 poll();
}

// The pipeline's four passes. Running one alone is "skip the other three",
// which is the same `skip` a whole-library run already takes — so a single
// pass on one title needs no endpoint of its own.
const PASSES = ['index', 'people', 'trivia', 'music', 'speakers'];

// contentId -> the passes ticked on that row. Outside the DOM because the
// store table is rebuilt on every poll, which would otherwise clear a
// selection every couple of seconds while the user was still making it.
const picked = new Map();

function togglePick(cid, pass){
 const sel = picked.get(cid) || new Set();
 if(sel.has(pass)) sel.delete(pass); else sel.add(pass);
 if(sel.size) picked.set(cid, sel); else picked.delete(cid);
 loadStore();          // repaint now; waiting for the poll feels broken
}

async function deepenRow(ratingKey, cid, label){
 const sel = picked.get(cid);
 // No selection means "fill every gap", which is what Deepen always did.
 // A selection means exactly those passes: skip is everything else.
 const skip = sel && sel.size
   ? PASSES.filter(p => !sel.has(p)).join(',')
   : runSkip(1);
 // Money is the only thing worth interrupting for. Confirm whenever music
 // will actually run — including the no-selection case, which used to bill
 // silently for anyone with an AudD token configured.
 const willBillForMusic = sel && sel.size
   ? sel.has('music')
   : (SETUP && SETUP.auddConfigured);
 if(willBillForMusic && !confirm(
      'Identify songs in ' + (label || 'this title') + '?\n\n'
      + 'Billed per music cue, about $0.005 each — roughly $0.10–$0.20 '
      + 'for a feature film. The other passes are free.')) return;
 const r = await post('api/run',
   {rating_key: ratingKey, level: 1, skip: skip});
 if(!r.ok) return alert((await r.json()).detail);
 picked.delete(cid);                  // the run owns it now
 $('results').innerHTML = ''; $('q').value = '';
 poll();
}

// ---- jobs ------------------------------------------------------------------

const STEP_LABEL = {index:'indexing', people:'cast', trivia:'trivia', music:'music'};
// Phase names are the pass's vocabulary; these are the viewer's.
const PHASE_LABEL = {frames:'reading the video', faces:'finding faces',
                     matching:'matching cast', writing:'writing'};

function phaseText(job){
 if(!job.phase) return 'working…';
 const label = PHASE_LABEL[job.phase] || job.phase;
 // Only the face pass knows its denominator; the rest are honest labels with
 // no number rather than a bar that invents one.
 return job.phaseTotal > 0
  ? label + ' ' + Math.floor(100 * job.phaseDone / job.phaseTotal) + '%'
  : label + '…';
}

async function poll(){
 const jobs = await j(await fetch('api/jobs'));
 const live = jobs.find(x => x.status === 'running' || x.status === 'queued');
 if(!live){
  const last = jobs[0];
  $('jobView').innerHTML = last
   ? '<div class="spread"><span class="meta">Last run: ' + esc(last.target || '')
     + ' · ' + esc(last.status) + ' · ' + last.done + '/' + last.total
     + '</span><button class="link sm" data-act="log" data-id="' + last.id
   + '">show log</button></div>'
   : '';
  loadStore();
  return;
 }
 // The log rides along only while it is open. Folded away, the strip needs
 // one line and a count, which log=0 now carries.
 const job = await j(await fetch('api/jobs?log=' + (logOpen ? 1 : 0)
   + '&id=' + live.id));
 const total = job.total || 0, done = (job.summary || []).length;
 // Within-title position folded into the overall bar, so one title creeps
 // forward instead of jumping 0→100, and a library run still measures titles.
 const frac = job.phaseFrac || 0;
 const pct = total ? (100 * Math.min(done + frac, total) / total).toFixed(1) : 0;
 // The rating key is what the user typed; the title is what they meant.
 const live_name = job.currentTitle || job.current;
 const rows = (job.summary || []).slice(-6).map(rowFor).join('')
  + (job.current && done < total
     ? '<div class="q live"><span class="ic pulse">●</span>'
       + '<span class="nm">' + esc(live_name) + '</span>'
       + '<span class="dt">' + esc(phaseText(job)) + '</span></div>' : '');
 // One title can only ever read 0/1 or 1/1, which looks stuck for the several
 // minutes a full index takes. Show the phase there and keep the count for
 // runs where it actually counts something.
 const counter = total > 1 ? done + ' / ' + total : esc(phaseText(job));
 $('jobView').innerHTML =
  '<div class="sec"><div class="spread"><h2>'
  + (job.request && job.request.level ? 'Indexing ' : 'Seeding ')
  + esc(total === 1 ? (live_name || job.target || '') : (job.target || ''))
  + '</h2><span class="mono">' + counter
  + ' <button class="link sm" data-act="stop" data-id="' + job.id + '">'
  + (job.cancel ? 'stopping…' : 'stop') + '</button>'
  + '</span></div><div class="track"><div class="fill" style="width:' + pct
  + '%"></div></div><div>' + rows + '</div>'
  + logStrip(job) + '</div>';
 loadStore();
}

// jobView is rebuilt from scratch on every poll, so "is the log open" has to
// live outside it or the panel would fold itself back up every few seconds.
let logOpen = false;

const LOG_TAIL = 14;

function logStrip(job){
 const n = job.logLines || (job.log || []).length;
 if(!n) return '';
 if(!logOpen){
  // Collapsed: newest line only. That line is the one carrying news, and a
  // count tells you how much you are not looking at.
  return '<div class="logbar" data-act="toglog"><span class="lg">'
   + '<i class="chev">&rsaquo;</i> ' + esc(job.lastLine || '') + '</span>'
   + '<span class="dt">' + n + ' line' + (n === 1 ? '' : 's') + '</span></div>';
 }
 const tail = (job.log || []).slice(-LOG_TAIL);
 return '<div class="logbar open" data-act="toglog"><span class="lg">'
  + '<i class="chev down">&rsaquo;</i> log</span>'
  + '<span class="dt">' + n + ' line' + (n === 1 ? '' : 's') + '</span></div>'
  + '<pre class="logtail">' + esc(tail.join('\n')) + '</pre>'
  + (n > LOG_TAIL
     ? '<div><button class="link sm" data-act="log" data-id="' + job.id
       + '">show all ' + n + '</button></div>' : '');
}

function rowFor(s){
 const steps = s.steps || {};
 const failed = Object.entries(steps).filter(([, v]) => String(v).startsWith('failed'));
 const noId = String(steps.index || '').includes('no content identity');
 if(noId) return '<div class="q bad"><span class="ic">!</span><span class="nm">'
  + esc(s.title) + '</span><span class="dt">no TMDb match · skipped</span></div>';
 // A pass that declined the title is not a failure and must not be styled as
 // one. 'skipped(flag)' is the user's own --skip and is deliberately excluded.
 const skipped = Object.entries(steps)
   .filter(([, v]) => String(v).startsWith('skipped: '));
 if(skipped.length && !failed.length){
  const why = String(skipped[0][1]).slice('skipped: '.length).split('—')[0].trim();
  return '<div class="q skip"><span class="ic">–</span><span class="nm">'
   + esc(s.title) + '</span><span class="dt">' + esc(why) + '</span></div>';
 }
 const cls = failed.length ? 'bad' : 'done';
 const detail = failed.length
  ? failed.map(([kk]) => kk).join(', ') + ' failed'
  : (steps.index === 'hub' ? 'from the hub' : esc(s.key));
 return '<div class="q ' + cls + '"><span class="ic">'
  + (failed.length ? '!' : '✓') + '</span><span class="nm">' + esc(s.title)
  + '</span><span class="dt">' + detail + '</span></div>';
}

async function stopJob(id){
 await post('api/jobs/' + id + '/stop', {});
 poll();   // reflect "stopping…" now; the worker lands it at the next marker
}

async function showLog(id){
 const jb = await j(await fetch('api/jobs?id=' + id));
 $('out').hidden = false;
 $('out').textContent = (jb.log || []).join('\n') || '(no log yet)';
}

// ---- store -----------------------------------------------------------------

async function loadStore(){
 const s = await j(await fetch('api/status'));
 // No AudD figure here. The 300 is AudD's one-time signup allowance and the
 // cap is ours, but the counter resets each calendar month, so "0/300 this
 // month" promised a recurring allowance that does not exist. The planner
 // still prices a run before it starts, which is where the number helps.
 $('stat').textContent = s.backend + ' · ' + (s.origin || 'no server');
 const seeds = s.titles.filter(t => !t.blocks.faces).length;
 const rows = s.titles.map(t => {
  const rk = (t.lookup[0] || '').split(':')[1];
  // A present block is a state; a missing one is an offer. Clicking an offer
  // TICKS it rather than running it: picking faces and music separately would
  // queue two jobs and stream the media twice, where one job harvests the
  // audio during frame extraction and the music pass reuses that file.
  //
  // Needs a server key: every pass runs through the pipeline against the
  // media server, so a timeline fetched from the hub with no local copy has
  // nothing to run against and stays a plain chip.
  const sel = picked.get(t.contentId) || new Set();
  const chip = (label, pass, on, paid) => {
   if (on) return '<span class="chip">' + label + '</span>';
   if (!rk) return '<span class="chip off">' + label + '</span>';
   const ticked = sel.has(pass);
   return '<button class="chip add' + (paid ? ' paid' : '')
    + (ticked ? ' on' : '') + '" aria-pressed="' + ticked + '"'
    + ' data-act="pick" data-cid="' + esc(t.contentId)
    + '" data-pass="' + pass + '"'
    + ' title="' + (paid
       ? 'Identify songs — billed per cue, about $0.10–0.20 for a feature'
       : 'Add ' + label + ' to this title — free') + '">'
    + (ticked ? '✓ ' : '+ ') + label + '</button>';
  };
  return '<tr><td>' + storeLabel(t)
   + '<div class="mono">' + esc(t.contentId) + '</div>'
   + '</td><td><div class="chips">'
   + chip('cast', 'people', t.blocks.people)
   + chip('faces', 'index', t.blocks.faces)
   + chip('music', 'music', t.blocks.music, true)
   + chip('trivia', 'trivia', t.blocks.trivia)
   // Only offered when the engine could actually do it. A tick that queues a
   // job destined to fail is worse than no tick: the refusal arrives later, in
   // a job log, instead of here where the question was asked. A title that has
   // ALREADY been diarized keeps its chip either way -- that one is a fact
   // about the timeline, not an offer.
   + (speakersOffered() || t.blocks.speakers
      ? chip('speakers', 'speakers', t.blocks.speakers) : '')
   // Never gated: naming clusters a previous run found is orchestrator work on
   // stored data, so it keeps working with the container stopped.
   + speakerNote(t)
   + '</div></td><td class="acts">'
   + speakerAction(t)
   // Offered whenever anything is missing, not just faces: with a selection
   // it runs exactly what is ticked, and with none it fills every gap.
   + ((rk && (!t.blocks.faces || !t.blocks.music || !t.blocks.people
              || !t.blocks.trivia))
      ? '<button class="ghost sm" data-act="deepen" data-rk="' + esc(rk)
        + '" data-cid="' + esc(t.contentId) + '" data-label="'
        + esc(storeLabel(t).replace(/<[^>]*>/g, '')) + '">'
        + (sel.size ? 'Run ' + sel.size + ' selected' : 'Deepen')
        + '</button> ' : '')
   + '<a class="ghost" href="api/export/' + encodeURIComponent(t.contentId)
   + '">export</a>'
   // Direct upload only where this machine holds a hub token; otherwise the
   // button could only ever report the hub's refusal, so it isn't offered.
   + (SETUP && SETUP.hubDirectUpload
      ? ' <button class="sm" data-act="share" data-cid="'
        + esc(t.contentId) + '">Share</button>' : '')
   + '</td></tr>';
 }).join('');
 const auto = SETUP && SETUP.hubAutoshare;
 $('storeView').innerHTML =
  '<div class="spread"><h2>Store</h2><span class="mono">'
  + plural(s.titles.length, 'timeline') + '</span></div>'
  // Auto-share can only work where direct upload can. Offering the toggle
  // otherwise promises a thing that ends in the hub's 403, once per title.
  + (SETUP && SETUP.hubUrl && SETUP.hubDirectUpload
     ? '<label class="row" style="font-size:13px"><input type="checkbox"'
       + (auto ? ' checked' : '') + ' onchange="setAutoshare(this.checked)">'
       + ' Share new timelines automatically'
       + '<span class="meta">' + (auto
          ? 'each title is sent for review as it finishes'
          : 'off: use Share per title') + '</span></label>'
     : '')
  + (SETUP && SETUP.hubUrl && !SETUP.hubDirectUpload && s.titles.length
     ? '<div class="note"><span>Contributing is two steps: build a bundle '
       + 'here, then upload it on the hub&rsquo;s contribute page. One bundle '
       + 'covers your whole store and counts as a single upload.</span>'
       + '<button class="sm" data-act="bundle">Export bundle</button></div>'
     : '')
  + '<div id="bundleOut" class="note" hidden></div>'
  + (s.titles.length
     ? '<div class="scroll"><table><thead><tr><th>Title</th><th>Contains</th>'
       + '<th></th></tr></thead><tbody>' + rows + '</tbody></table></div>'
       + (seeds ? '<div class="note"><span><b>' + plural(seeds, 'title')
          + '</b> ' + (seeds === 1 ? 'has' : 'have') + ' no face intervals '
          + 'yet. Deepen to add them'
          + (SETUP && SETUP.auddConfigured ? ' and the song names' : '')
          + '.</span></div>' : '')
     : '<p class="sub">Nothing indexed yet.</p>')
  + '<div class="row"><input id="importSrc" style="flex:1" '
  + 'placeholder="import URL (hub or shared file)">'
  + '<button class="ghost" onclick="doImport()">Import</button>'
  + '<button class="ghost" onclick="doValidate()">Validate all</button></div>';
 paintBundle();   // the markup above just recreated an empty #bundleOut
}

// Season/episode come from the contentId, not the doc: one source of truth,
// so the label can never disagree with the identity the file is stored under.
const EP_RE = /^tmdb-tv-\d+-s(\d{2})e(\d{2})$/;

// A diarized title is NOT finished: the pass stores clusters and stops,
// because naming a speaker needs a person. Every other block means done, so
// this is the one that has to say what is still owed.
// The speakers STATE, as a chip among the other fact-chips. Leads with the
// share of dialogue named rather than the speaker count: five names out of
// 24 sounds 20% done, but dialogue is top-heavy — on the first real title
// those five covered 47%. Counting speakers understates; minutes don't.
//: Why-does-this-exist, one hover away. The chip says 47% named; nothing
//: says why a movie needs its speakers named at all, and the answer is the
//: whole animation story compressed to two sentences.
const SPK_WHY = 'Animation has no faces to recognise, so this timeline is '
 + 'built from voices. Name a speaker and their lines fill in; names carry '
 + 'to other titles with the same cast.';

function speakerNote(t){
 const s = t.speakerState;
 if(!s) return '';
 // INSIDE the chip, not after it: the chips row flex-wraps, and a trailing
 // sibling is first to be orphaned onto its own line. As a flex child of the
 // pill it can never separate from the thing it explains.
 //
 // data-tip, NOT title. The store table repaints on every poll, and a native
 // tooltip needs ~a second of hover on an element that lives four -- it
 // mostly never appeared. The CSS tooltip is instant, and tabindex makes a
 // CLICK show it too (focus), which is what people try first anyway.
 const info = '<span class="infodot" tabindex="0" data-tip="' + SPK_WHY
  + '">i</span>';
 if(!s.nameable)
   return '<span class="spkstate">voices<span class="mini"><i></i></span>'
    + 'none nameable' + info + '</span>';
 const done = s.named >= s.nameable;
 const bar = '<span class="mini"><i style="width:' + (s.pct || 0)
  + '%"></i></span>';
 if(done)
   return '<span class="spkstate ok">voices' + bar + (s.pct || 0)
    + '% named · ' + s.named + '/' + s.nameable + info + '</span>';
 return '<span class="spkstate owed">voices' + bar + (s.pct || 0)
  + '% named · ' + s.named + '/' + s.nameable + info + '</span>';
}

// The speakers ACTION, in the acts column where actions live. The old design
// hid the only click-me in the app inside an underlined mono string in the
// facts row, and it read as a log line. A button next to Deepen does not.
function speakerAction(t){
 const s = t.speakerState;
 if(!s || !s.nameable || s.named >= s.nameable) return '';
 return '<button class="namebtn" data-act="label" data-cid="'
  + esc(t.contentId) + '" title="Play each speaker and pick who it is">'
  + 'Name speakers<span class="n">' + (s.nameable - s.named)
  + '</span></button> ';
}


function storeLabel(t){
 if(!t.title) return '<b>' + esc(t.contentId) + '</b>';
 const m = EP_RE.exec(t.contentId);
 if(t.series && m) return '<b>' + esc(t.series) + '</b> S' + m[1] + 'E' + m[2]
  + ' · ' + esc(t.title);
 if(t.series) return '<b>' + esc(t.series) + '</b> · ' + esc(t.title);
 return '<b>' + esc(t.title) + '</b>'
  + (t.year ? ' <span class="meta">(' + esc(t.year) + ')</span>' : '');
}

async function setAutoshare(on){
 // Empty string deletes the key: settings_store's own off switch.
 await fetch('api/settings', {method:'PUT',
  headers:{'content-type':'application/json'},
  body: JSON.stringify({hub_autoshare: on ? 'on' : ''})});
 SETUP.hubAutoshare = on;
 loadStore();
}

async function hubUpload(cid){
 const r = await post('api/hub/upload/' + encodeURIComponent(cid));
 $('out').hidden = false;
 $('out').textContent = JSON.stringify(await r.json(), null, 1);
}
// The Store section is rebuilt every poll, which used to wipe the download
// links a few seconds after the bundle was written. Keep the rendered result
// here and let loadStore paint it back.
let bundleResult = '';

function paintBundle(){
 const box = $('bundleOut');
 if(!box) return;
 box.hidden = !bundleResult;
 box.innerHTML = bundleResult;
}

async function exportBundle(btn){
 btn.disabled = true; btn.textContent = 'Bundling…';
 bundleResult = '<span>Writing the bundle…</span>';
 paintBundle();
 try {
  const r = await post('api/export/bundle', {contentIds: []});
  const d = await r.json();
  if(!r.ok){
   bundleResult = '<span>' + esc((d.detail && d.detail.reason) || d.detail
     || 'export failed') + '</span>';
   paintBundle();
   return;
  }
  // Chunked when a hub would refuse the whole thing; each file uploads
  // separately, so link every one rather than only the first.
  const kb = Math.round(d.bytes / 1024);
  const links = d.files.map(n => '<a class="ghost" href="api/export/bundle/'
    + encodeURIComponent(n) + '">' + esc(n) + '</a>').join(' ');
  bundleResult = '<span>' + plural(d.timelines, 'timeline') + ' in '
   + plural(d.files.length, 'file') + ' (' + kb + ' KB). Download, then upload '
   + 'at <a href="' + esc((SETUP.hubUrl || '').replace(/\/$/, ''))
   + '/contribute" target="_blank" rel="noopener noreferrer" rel="noopener noreferrer">the hub&rsquo;s '
   + 'contribute page</a>.</span><span>' + links + '</span>';
  paintBundle();
 } finally {
  btn.disabled = false; btn.textContent = 'Export bundle';
 }
}
async function doImport(){
 const src = $('importSrc').value.trim();
 if(!src) return;
 const r = await post('api/import', {src});
 $('out').hidden = false;
 $('out').textContent = JSON.stringify(await r.json(), null, 1);
 loadStore();
}
async function doValidate(){
 const r = await j(await fetch('api/validate'));
 $('out').hidden = false;
 $('out').textContent = r.results.map(x =>
  x.file + ': ' + (x.valid ? 'VALID' : 'INVALID: ' + x.error)).join('\n')
  || '(store is empty)';
}

loadSetup();
setInterval(poll, 4000);
