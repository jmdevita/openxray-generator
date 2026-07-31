// The labelling screen: the one place a person supplies data rather than
// reading it. Diarization found N anonymous speakers; this attaches names.
//
// Manual save on purpose. Auto-saving on every pick makes opening a dropdown
// to look around a destructive act, and there is no way back from a misclick.
// Picks are staged, the bar says how many are outstanding, and nothing
// reaches the server until save.

let LAB = null;                  // the loaded screen state
let LAB_CID = null;              // which title we are naming
let LAB_PENDING = {};            // speaker -> {actorId, character, sim} | null
let LAB_AUDIO = null, LAB_PLAYING = null;

const labCount = () => Object.keys(LAB_PENDING).length;
// What a row should DISPLAY: a staged pick wins over what is saved.
const labShown = r => (r.speaker in LAB_PENDING)
  ? LAB_PENDING[r.speaker] : r.assigned;

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

async function openLabelling(cid){
 LAB_CID = cid; LAB_PENDING = {}; LAB_PLAYING = null;
 labShow(true);
 $('labelView').innerHTML = LAB_SHELL;
 $('labelBody').innerHTML = '<p class="sub">loading speakers…</p>';
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
 + '<div class="savebar" id="labelBar">'
 +  '<span class="mono" id="labelPend">all changes saved</span>'
 +  '<span><button class="ghost sm" id="labelDiscard" style="display:none">'
 +   'discard</button> <button class="sm" id="labelSave">save</button></span>'
 + '</div>';

async function loadLabelling(){
 const r = await fetch('api/speakers/' + encodeURIComponent(LAB_CID));
 if(!r.ok){
   $('labelBody').innerHTML = '<p class="sub">'
     + esc((await r.json()).detail || 'could not load speakers') + '</p>';
   return;
 }
 LAB = await r.json();
 paintLabelling();
}

function paintLabelling(){
 const rows = LAB.rows;
 const nameable = rows.filter(r => r.enrollable);
 const named = rows.filter(labShown).length;
 const namedSecs = rows.filter(labShown)
                       .reduce((a, r) => a + r.seconds, 0);
 const pct = LAB.speechSeconds
   ? Math.round(namedSecs / LAB.speechSeconds * 100) : 0;

 $('labelTitle').textContent = LAB.title || LAB.contentId;
 $('labelSub').textContent =
   named + ' of ' + nameable.length + ' named · '
   + Math.round(namedSecs / 60) + ' of ' + Math.round(LAB.speechSeconds / 60)
   + ' minutes of dialogue' + (labCount() ? ' · ' + labCount() + ' unsaved' : '');
 $('labelProg').style.width = pct + '%';

 const over = rows.filter(r => r.enrollable);
 const under = rows.filter(r => !r.enrollable);
 $('labelBody').innerHTML = over.map(labRow).join('')
   + (under.length
      ? '<p class="sub tiny">' + under.length + ' speakers under the '
        + Math.round(LAB.enrollMin) + ' second floor: too little speech for a '
        + 'reliable voiceprint, so they are neither named nor matched.</p>'
      : '');

 const n = labCount();
 $('labelBar').classList.toggle('dirty', n > 0);
 $('labelPend').textContent = n
   ? n + ' unsaved change' + (n === 1 ? '' : 's') : 'all changes saved';
 $('labelDiscard').style.display = n ? 'inline-block' : 'none';
 wireLabelling();
}

function labRow(r){
 const role = labShown(r);
 const dirty = (r.speaker in LAB_PENDING);
 const blocks = (r.spans || []).map(sp =>
   '<i style="left:' + (sp[0] * 100).toFixed(3) + '%;width:'
   + Math.max(sp[1] * 100, 0.35).toFixed(3) + '%"></i>').join('');

 let who;
 if(role){
   who = '<button class="chip on" data-act="labedit" data-spk="'
     + esc(r.speaker) + '">' + esc(role.character) + '</button>'
     + (dirty ? '<span class="spknote owed">unsaved</span>' : '');
 } else if(r.suggest){
   who = '<button class="chip sug" data-act="labtake" data-spk="'
     + esc(r.speaker) + '" data-actor="' + esc(r.suggest.actorId)
     + '" data-char="' + esc(r.suggest.character) + '" data-sim="'
     + r.suggest.sim + '">' + esc(r.suggest.character) + ' · '
     + r.suggest.sim + '</button>'
     + '<span class="spknote tiny">recognised from another title</span>';
 } else {
   who = labPicker(r);
 }

 return '<div class="labrow' + (role ? ' named' : '')
  + (dirty ? ' dirty' : '') + (LAB_PLAYING === r.speaker ? ' on' : '') + '">'
  + '<button class="play" data-act="labplay" data-spk="' + esc(r.speaker)
  + '">' + (LAB_PLAYING === r.speaker ? '❚❚' : '▶') + '</button>'
  + '<div class="labmid"><div class="strip">' + blocks + '</div>'
  + '<div class="mono tiny">'
  + (LAB_PLAYING === r.speaker ? 'playing' : labSecs(r.seconds))
  + ' · ' + (r.spans || []).length + ' scenes'
  + (r.matchable ? '' : ' · too short to match') + '</div></div>'
  + '<div class="labwho">' + who + '</div></div>';
}

function labPicker(r, selected){
 // The title's own TMDb cast, so a pick is an entity with an id rather than
 // typed text something downstream would have to reconcile.
 return '<select data-act="labpick" data-spk="' + esc(r.speaker) + '">'
  + '<option value="">name this speaker…</option>'
  + LAB.cast.map(c => '<option value="' + esc(c.actorId) + '"'
      + (selected === c.actorId ? ' selected' : '') + '>'
      + esc(c.character || c.name) + (c.character ? ' · ' + esc(c.name) : '')
      + '</option>').join('')
  + '</select>';
}

function wireLabelling(){
 document.querySelectorAll('[data-act="labplay"]').forEach(b =>
   b.onclick = () => labPlay(b.dataset.spk));
 document.querySelectorAll('[data-act="labpick"]').forEach(s =>
   s.onchange = () => {
     const c = LAB.cast.find(x => x.actorId === s.value);
     labStage(s.dataset.spk, c ? {actorId: c.actorId,
                                  character: c.character || c.name} : null);
   });
 document.querySelectorAll('[data-act="labtake"]').forEach(b =>
   b.onclick = () => labStage(b.dataset.spk, {
     actorId: b.dataset.actor, character: b.dataset.char,
     sim: parseFloat(b.dataset.sim)}));
 document.querySelectorAll('[data-act="labedit"]').forEach(b =>
   b.onclick = () => {
     const r = LAB.rows.find(x => x.speaker === b.dataset.spk);
     const cur = labShown(r);
     b.parentNode.innerHTML = labPicker(r, cur && cur.actorId);
     wireLabelling();
   });
}

function labStage(speaker, role){
 // Re-picking what is already saved drops the entry instead of recording a
 // no-op, so the counter never overstates what is outstanding.
 const row = LAB.rows.find(r => r.speaker === speaker);
 const savedId = row && row.assigned ? row.assigned.actorId : null;
 if((role ? role.actorId : null) === savedId) delete LAB_PENDING[speaker];
 else LAB_PENDING[speaker] = role;
 paintLabelling();
}

function labPlay(speaker){
 if(LAB_PLAYING === speaker && LAB_AUDIO && !LAB_AUDIO.paused){
   LAB_AUDIO.pause(); LAB_PLAYING = null; paintLabelling(); return;
 }
 if(LAB_AUDIO) LAB_AUDIO.pause();
 LAB_AUDIO = new Audio('api/speakers/' + encodeURIComponent(LAB_CID)
                       + '/clip/' + encodeURIComponent(speaker));
 LAB_PLAYING = speaker;
 LAB_AUDIO.onended = () => { LAB_PLAYING = null; paintLabelling(); };
 LAB_AUDIO.play();
 paintLabelling();
}

async function labSave(){
 const n = labCount();
 for(const [speaker, role] of Object.entries(LAB_PENDING)){
   await post('api/speakers/' + encodeURIComponent(LAB_CID) + '/name',
     role ? {speaker: speaker, actor_id: role.actorId,
             character: role.character, sim: role.sim || null}
          : {speaker: speaker});
 }
 LAB_PENDING = {};
 await loadLabelling();
 loadStore();                      // the row's owed count just changed
 $('out').textContent = 'saved ' + n + ' name'
   + (n === 1 ? '' : 's') + ' for ' + LAB_CID;
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
