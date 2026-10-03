import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {build} from 'esbuild';
import {fileURLToPath} from 'node:url';
async function moduleAt(name){const bundle=await build({entryPoints:[fileURLToPath(new URL('../src/'+name,import.meta.url))],bundle:true,write:false,platform:'node',format:'cjs',jsx:'automatic',external:['react','react/jsx-runtime']});const mod={exports:{}};new Function('module','exports','require',bundle.outputFiles[0].text)(mod,mod.exports,createRequire(import.meta.url));return mod.exports;}
const {supportedDestinations}=await moduleAt('App.jsx');
const {detailRows}=await moduleAt('Pipeline.jsx');
test('Local-only board overrides AWS and onprem configuration',()=>{assert.deepEqual(supportedDestinations({aws_enabled:true,onprem_enabled:true},['local']),{aws:false,onprem:false,both:false,none:true});});
test('existing samples keep configured targets',()=>{assert.equal(supportedDestinations({aws_enabled:true},['local','aws','onprem']).aws,true);});
test('worker evidence summary requires actual verification record',()=>{let rows=detailRows(JSON.stringify({assets:{code:'board_assets_verified',file_count:10}}));assert.equal(rows.length,1);rows=detailRows(JSON.stringify({assets:{code:'board_assets_verified',file_count:10},worker:{code:'board_worker_verified',probe_note_id:7,minimum_count:4,observed_count:5,observed_at:'2026-10-03T00:00:00Z'}}));assert.ok(rows.some(r=>r[1].includes('기대 4건 이상 / 관찰 5건')));});
