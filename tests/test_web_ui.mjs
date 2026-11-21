// Static smoke checks for critical app surfaces. Does not replace a real-browser E2E test.
import fs from 'node:fs';
import assert from 'node:assert/strict';
const html=fs.readFileSync('web/index.html','utf8');
const markup=html.split('<script type="module">')[0];
const ids=[...markup.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
for(const id of ['vProjects','projSummary','projSearch','projFilter','projFolder','projSort',
  'projLayout','projList','projMore','projUpgrade','vCreator','creatorStats','creatorPixels',
  'pixelSelected','pixelGrid','vApp','results','grid','exportAll']){
  assert(ids.includes(id), 'Missing important UI element: '+id);
}
assert(html.includes('id="navLevel"'),'Dynamic navigation level badge missing');
const seen=new Set();
for(const id of ids){assert(!seen.has(id),'Duplicate markup id: '+id);seen.add(id);}
const part=html.match(/const TEMPLATES\s*=\s*\[([\s\S]*?)\];/);
assert(part,'Template declaration not found');
const templateIds=[...part[1].matchAll(/\bid\s*:\s*['"]([^'"]+)['"]/g)].map(m=>m[1]);
assert.deepEqual(templateIds,['stream','stream_nt','raw']);
assert(html.includes("data-pj=\"favorite\"")||html.includes("'favorite'"),'Favorites missing');
assert(html.includes("/api/creator/avatar"),'Character persistence endpoint missing');
assert(html.includes('function renderPixelRoster('),'Pixel roster renderer missing');
const open=html.slice(html.indexOf('async function openProject('),html.indexOf('// ---------- conta ----------'));
assert(!open.includes('loadASR('),'Saved projects must not reload transcription');
assert(!open.includes('await audioFeatures('),'Saved projects must open before waveform extraction');
assert(html.includes('ensureLibraryAudio(false)'),'Fallback audio loading missing');
console.log('Project library, templates and pixel character UI checks passed.');
