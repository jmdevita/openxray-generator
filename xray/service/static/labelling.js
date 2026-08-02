// The labelling screen: the one place a person supplies data rather than
// reading it. One screen, two senses -- diarization found anonymous speakers,
// the face pass found clusters it could not name -- because the job is
// identical either way: look at a thing, say who it is.
//
// Everything except HOW a row is previewed is shared. That is deliberate: the
// staging model below is fiddly and worth having exactly one of.
//
// Manual save on purpose. Auto-saving on every pick makes opening a dropdown
// to look around a destructive act, and there is no way back from a misclick.
// Picks are staged, the bar says how many are outstanding, and nothing
// reaches the server until save.

let LAB = null;                  // the loaded screen state
let LAB_CID = null;              // which title we are naming
let LAB_K = null;                // which KIND of thing we are naming
let LAB_PENDING = {};            // row id -> {actorId, character, sim} | null
let LAB_AUDIO = null, LAB_PLAYING = null;

// What differs between the two, and nothing else does.
const LAB_KINDS = {
 speakers: {
  api: 'api/speakers',
  field: 'speaker',                       // the POST body's id field
  id: r => r.speaker,
  noun: 'speaker', material: 'dialogue',
  total: L => L.speechSeconds,
  // An audition clip: several passages from across the runtime, so a cluster
  // that merged two characters is audible as the voice changing partway.
  preview: r => '<button class="play" data-act="labplay" data-row="'
    + esc(String(r.speaker)) + '">'
    + (LAB_PLAYING === String(r.speaker) ? '❚❚' : '▶') + '</button>',
  stats: r => (LAB_PLAYING === String(r.speaker) ? 'playing' : labSecs(r.seconds))
    + ' · ' + (r.spans || []).length + ' scenes'
    + (r.matchable ? '' : ' · too short to match'),
  floor: L => 'speakers under the ' + Math.round(L.enrollMin)
    + ' second floor: too little speech for a reliable voiceprint, so they '
    + 'are neither named nor matched.',
 },
 faces: {
  api: 'api/faces',
  field: 'cluster',
  id: r => r.cluster,
  noun: 'face', material: 'screen time',
  total: L => L.screenSeconds,
  // Three faces from across the cluster's life, for the same reason the
  // audio clip is stitched from three places: an impure cluster shows up as
  // the face changing halfway along the strip.
  preview: r => '<img class="labcrop" loading="lazy" alt="" src="'
    + 'api/faces/' + encodeURIComponent(LAB_CID) + '/crop/'
    + encodeURIComponent(r.cluster) + '">',
  stats: r => labSecs(r.seconds) + ' · ' + r.scenes + ' scenes',
  floor: L => 'clusters under ' + Math.round(L.minSeconds) + ' seconds or '
    + 'appearing in fewer than ' + L.minScenes + ' scenes: extras, blurred '
    + 'background faces, and the occasional lamp.',
 },
 // Music is the speakers shape with a different question: you listen, then
 // you TYPE. There is no list to pick from — a song is not a person, and
 // nothing in the timeline knows the soundtrack in advance.
 music: {
  api: 'api/music',
  field: 'cue',
  id: r => r.cue,
  noun: 'cue', material: 'music',
  total: L => L.musicSeconds,
  freeText: true,
  preview: r => '<button class="play" data-act="labplay" data-row="'
    + esc(String(r.cue)) + '">'
    + (LAB_PLAYING === String(r.cue) ? '❚❚' : '▶') + '</button>',
  // WHERE it happens matters here in a way it does not for a face: you are
  // about to go and listen to that moment in the film.
  stats: r => (LAB_PLAYING === String(r.cue) ? 'playing' : labSecs(r.seconds))
    + ' · at ' + labClock(r.startMs),
  floor: () => '',   // no floor: every cue the segmenter kept is nameable
 },
};

// mm:ss position of a cue in the film.
function labClock(ms){
 const t = Math.floor((ms || 0) / 1000);
 return Math.floor(t / 60) + ':' + String(t % 60).padStart(2, '0');
}

const labCount = () => Object.keys(LAB_PENDING).length;
const labId = r => String(LAB_K.id(r));
// What a row should DISPLAY, in priority order: a staged pick beats what is
// saved, which beats what the pass matched on its own.
const labShown = r => (labId(r) in LAB_PENDING)
  ? LAB_PENDING[labId(r)] : (r.assigned || r.matched || null);
// "Settled" for progress: an automatic match already puts intervals in the
// timeline, so it counts as done even though nobody has confirmed it.
const labSettled = r => !!labShown(r);

function labSecs(s){
 const m = Math.floor(s / 60);
 return m ? m + 'm ' + Math.round(s % 60) + 's' : Math.round(s) + 's';
}

// Views are switched by setting .hidden on the section divs; there is no
// router. Matching that rather than inventing one keeps the setup gate in
// loadSetup() the single place that decides what is visible.
function labShow(on){
 $('runView').hidden = on;
 $('storeView').hidden = on;
 $('labelView').hidden = !on;
}

async function openLabelling(cid, kind){
 LAB_CID = cid; LAB_K = LAB_KINDS[kind || 'speakers'];
 LAB_PENDING = {}; LAB_PLAYING = null;
 labShow(true);
 $('labelView').innerHTML = LAB_SHELL;
 $('labelBody').innerHTML = '<p class="sub">loading ' + LAB_K.noun + 's…</p>';
 $('labelBack').onclick = labLeave;
 $('labelSave').onclick = labSave;
 $('labelDiscard').onclick = labDiscard;
 await loadLabelling();
}

//: Injected rather than living in dashboard.html: the screen only exists
//: once someone opens it, and an empty shell in the page would be one more
//: thing for the setup gate to remember to hide.
const LAB_SHELL =
 '<button class="link sm" id="labelBack">← back to titles</button>'
 + '<h2 id="labelTitle"></h2>'
 + '<p class="sub" id="labelSub"></p>'
 + '<div class="bar"><i id="labelProg" style="width:0%"></i></div>'
 + '<div id="labelBody"></div>'
 + '<div class="spread notice" id="labelSpread" hidden></div>'
 + '<div class="savebar" id="labelBar">'
 +  '<span class="mono" id="labelPend">all changes saved</span>'
 +  '<span><button class="ghost sm" id="labelDiscard" style="display:none">'
 +   'discard</button> <button class="sm" id="labelSave">save</button></span>'
 + '</div>';

async function loadLabelling(){
 const r = await fetch(LAB_K.api + '/' + encodeURIComponent(LAB_CID));
 if(!r.ok){
   $('labelBody').innerHTML = '<p class="sub">'
     + esc((await r.json()).detail || ('could not load ' + LAB_K.noun + 's'))
     + '</p>';
   return;
 }
 LAB = await r.json();
 paintLabelling();
}

function paintLabelling(){
 const rows = LAB.rows;
 // Speakers arrive with the below-floor ones included and filtered here;
 // faces are filtered server-side and counted instead.
 const over = rows.filter(r => r.enrollable === undefined || r.enrollable);
 const under = rows.length - over.length + (LAB.belowFloor || 0);
 const settled = over.filter(labSettled);
 const covered = settled.reduce((a, r) => a + r.seconds, 0);
 const total = LAB_K.total(LAB) || 0;
 const pct = total ? Math.round(covered / total * 100) : 0;

 $('labelTitle').textContent = LAB.title || LAB.contentId;
 $('labelSub').textContent =
   settled.length + ' of ' + over.length + ' named · '
   + Math.round(covered / 60) + ' of ' + Math.round(total / 60)
   + ' minutes of ' + LAB_K.material
   + (labCount() ? ' · ' + labCount() + ' unsaved' : '');
 $('labelProg').style.width = pct + '%';

 $('labelBody').innerHTML = over.map(labRow).join('')
   + (under ? '<p class="sub tiny">' + under + ' ' + LAB_K.floor(LAB) + '</p>'
            : '');

 const n = labCount();
 $('labelBar').classList.toggle('dirty', n > 0);
 $('labelPend').textContent = n
   ? n + ' unsaved change' + (n === 1 ? '' : 's') : 'all changes saved';
 $('labelDiscard').style.display = n ? 'inline-block' : 'none';
 wireLabelling();
}

// How strong a machine's guess is, in words, with the score and the evidence
// for reading it that way one hover below. Both come from the server, which
// is where the measurements live. Never a percentage: the score is not a
// probability, and 0.55 -- the line above which every checked match was
// right -- would read as a coin flip.
function labSim(m, note){
 const word = m.confidence || 'matched';
 return '<span class="spknote tiny" tabindex="0" data-tip="'
  + esc(m.explain || String(m.sim)) + '">' + esc(word) + ' · ' + note
  + '</span>';
}

function labRow(r){
 const id = labId(r);
 const role = labShown(r);
 const dirty = (id in LAB_PENDING);
 const auto = !dirty && !r.assigned && r.matched;   // machine's guess, unconfirmed
 const blocks = (r.spans || []).map(sp =>
   '<i style="left:' + (sp[0] * 100).toFixed(3) + '%;width:'
   + Math.max(sp[1] * 100, 0.35).toFixed(3) + '%"></i>').join('');

 let who;
 if(role){
   who = '<button class="chip' + (auto ? ' auto' : ' on')
     + '" data-act="labedit" data-row="' + esc(id) + '">'
     + esc(LAB_K.freeText
            ? role.title + (role.artist ? ' · ' + role.artist : '')
            : role.character) + '</button>'
     + (dirty ? '<span class="spknote owed">unsaved</span>'
        // An automatic match is a claim nobody has checked. One at 0.373 put
        // two minutes of one actor under another's name, and only a person
        // looking at the face would ever have caught it.
        : auto ? labSim(r.matched, 'check me') : '');
 } else if(r.suggest){
   who = '<button class="chip sug" data-act="labtake" data-row="' + esc(id)
     + '" data-actor="' + esc(r.suggest.actorId) + '" data-char="'
     + esc(r.suggest.character) + '" data-sim="' + r.suggest.sim + '">'
     + esc(r.suggest.character) + '</button>'
     + labSim(r.suggest, 'recognised from another title');
 } else {
   who = labPicker(r);
 }

 return '<div class="labrow' + (role ? ' named' : '')
  + (dirty ? ' dirty' : '') + (LAB_PLAYING === id ? ' on' : '') + '">'
  + LAB_K.preview(r)
  + '<div class="labmid"><div class="strip">' + blocks + '</div>'
  + '<div class="mono tiny">' + LAB_K.stats(r) + '</div></div>'
  + '<div class="labwho">' + who + '</div></div>';
}

function labPicker(r, selected){
 // Music has nothing to pick FROM: the timeline knows the cast in advance
 // and never knows the soundtrack, so this one is two text fields.
 if(LAB_K.freeText){
  const cur = labShown(r) || {};
  return '<span class="labtext">'
   + '<input data-act="labtitle" data-row="' + esc(labId(r)) + '"'
   + ' placeholder="song title" value="' + esc(cur.title || '') + '">'
   + '<input data-act="labartist" data-row="' + esc(labId(r)) + '"'
   + ' placeholder="artist" value="' + esc(cur.artist || '') + '"></span>';
 }
 // The title's own cast, so a pick is an entity with an id rather than typed
 // text something downstream would have to reconcile.
 return '<select data-act="labpick" data-row="' + esc(labId(r)) + '">'
  + '<option value="">name this ' + LAB_K.noun + '…</option>'
  + LAB.cast.map(c => '<option value="' + esc(c.actorId) + '"'
      + (selected === c.actorId ? ' selected' : '') + '>'
      + esc(c.character || c.name) + (c.character ? ' · ' + esc(c.name) : '')
      + '</option>').join('')
  + '</select>';
}

function labFind(id){
 return LAB.rows.find(r => labId(r) === id);
}

function wireLabelling(){
 document.querySelectorAll('[data-act="labplay"]').forEach(b =>
   b.onclick = () => labPlay(b.dataset.row));
 document.querySelectorAll('[data-act="labpick"]').forEach(s =>
   s.onchange = () => {
     const c = LAB.cast.find(x => x.actorId === s.value);
     labStage(s.dataset.row, c ? {actorId: c.actorId,
                                  character: c.character || c.name} : null);
   });
 // Staged on `change` (blur / Enter), not on every keystroke: a per-character
 // stage would count "S", "Se", "Sex"… as edits and make the Save counter
 // meaningless.
 document.querySelectorAll('[data-act="labtitle"],[data-act="labartist"]')
   .forEach(i => i.onchange = () => {
     const row = i.dataset.row;
     const box = i.parentNode;
     const title = box.querySelector('[data-act="labtitle"]').value.trim();
     const artist = box.querySelector('[data-act="labartist"]').value.trim();
     labStage(row, title ? {title: title, artist: artist} : null);
   });
 document.querySelectorAll('[data-act="labtake"]').forEach(b =>
   b.onclick = () => labStage(b.dataset.row, {
     actorId: b.dataset.actor, character: b.dataset.char,
     sim: parseFloat(b.dataset.sim)}));
 document.querySelectorAll('[data-act="labedit"]').forEach(b =>
   b.onclick = () => {
     const r = labFind(b.dataset.row);
     const cur = labShown(r);
     b.parentNode.innerHTML = labPicker(r, cur && cur.actorId);
     wireLabelling();
   });
}

function labStage(id, role){
 // Re-picking what is already saved drops the entry instead of recording a
 // no-op, so the counter never overstates what is outstanding. An automatic
 // match is NOT "already saved": confirming one is a real edit, because it
 // promotes a guess to a human's word.
 const row = labFind(id);
 const savedId = row && row.assigned ? row.assigned.actorId : null;
 if((role ? role.actorId : null) === savedId && savedId !== null)
   delete LAB_PENDING[id];
 else LAB_PENDING[id] = role;
 paintLabelling();
}

function labPlay(id){
 if(LAB_PLAYING === id && LAB_AUDIO && !LAB_AUDIO.paused){
   LAB_AUDIO.pause(); LAB_PLAYING = null; paintLabelling(); return;
 }
 if(LAB_AUDIO) LAB_AUDIO.pause();
 LAB_AUDIO = new Audio(LAB_K.api + '/' + encodeURIComponent(LAB_CID)
                       + '/clip/' + encodeURIComponent(id));
 LAB_PLAYING = id;
 LAB_AUDIO.onended = () => { LAB_PLAYING = null; paintLabelling(); };
 LAB_AUDIO.play();
 paintLabelling();
}

async function labSave(){
 const n = labCount();
 let last = null;
 for(const [id, role] of Object.entries(LAB_PENDING)){
   const body = {};
   // Numeric ids stay numeric: `cluster` and `cue` are ints server-side,
   // `speaker` is a label like "SPEAKER_03".
   body[LAB_K.field] = LAB_K.field === 'speaker' ? id : parseInt(id, 10);
   if(LAB_K.freeText){
     // An empty title is how a row is CLEARED, so it is sent, not skipped.
     body.title = (role && role.title) || '';
     body.artist = (role && role.artist) || '';
   } else if(role){
     body.actor_id = role.actorId;
     body.character = role.character;
     body.sim = role.sim || null;
   }
   last = await post(LAB_K.api + '/' + encodeURIComponent(LAB_CID)
                     + '/name', body);
 }
 LAB_PENDING = {};
 await loadLabelling();
 loadStore();                      // the row's owed count just changed
 $('out').textContent = 'saved ' + n + ' name'
   + (n === 1 ? '' : 's') + ' for ' + LAB_CID;
 $('out').hidden = false;
 labOfferSpread(last);
}

// A button, not an automatic act: quietly rewriting eleven other episodes
// is unforgivable the first time it is wrong. Faces only -- voiceprints
// already carry across titles and have no series to be confined to.
function labOfferSpread(res){
 const n = res && res.siblings;
 if(!n || LAB_K.api !== 'api/faces') return;
 $('labelSpread').hidden = false;
 $('labelSpread').innerHTML =
   '<span>' + n + ' more episode' + (n === 1 ? '' : 's')
   + ' of this series ' + (n === 1 ? 'has' : 'have') + ' been indexed. '
   + 'Carry these names across?</span> '
   + '<button class="sm" data-act="labspread">Name them too</button>';
 $('labelSpread').querySelector('[data-act="labspread"]').onclick = labSpread;
}

async function labSpread(){
 const btn = $('labelSpread').querySelector('button');
 btn.disabled = true;
 btn.textContent = 'looking…';
 const r = await post('api/faces/' + encodeURIComponent(LAB_CID)
                      + '/propagate', {});
 $('labelSpread').hidden = true;
 loadStore();
 $('out').textContent = r.named
   ? 'named ' + r.named + ' face' + (r.named === 1 ? '' : 's') + ' across '
     + r.episodes + ' episode' + (r.episodes === 1 ? '' : 's')
     + ' — check them on their own screens'
   : 'nothing in the other episodes matched closely enough';
 $('out').hidden = false;
}

function labDiscard(){ LAB_PENDING = {}; paintLabelling(); }

function labLeave(){
 if(labCount() && !confirm(labCount()
    + ' unsaved name(s) will be discarded. Leave anyway?')) return;
 LAB_PENDING = {};
 if(LAB_AUDIO) LAB_AUDIO.pause();
 labShow(false);
}
