import {readFile} from 'node:fs/promises';
import test from 'node:test';
import assert from 'node:assert/strict';
import React from 'react';
import {createRequire} from 'node:module';
import {renderToStaticMarkup} from 'react-dom/server';
import {transformWithEsbuild} from 'vite';
const src=await readFile(new URL('../src/PolicyDocument.jsx',import.meta.url),'utf8');
const {code}=await transformWithEsbuild(src,'PolicyDocument.jsx',{jsx:'transform',jsxFactory:'React.createElement',format:'cjs'});
const mod={exports:{}};
new Function('module','React','require',code)(mod,React,createRequire(import.meta.url));
const {default:Document,documentHeadings,policyDocLink,policyVersionRoute}=mod.exports;
const context={version:'1.0.0',slug:'rules/I-001'};
const render=body=>renderToStaticMarkup(React.createElement(Document,{body,...context}));
test('tables lists and code render semantic elements',()=>{
 const html=render('# Title\n\n## Inputs {#inputs}\n\n1. First\n2. Second\n\n| Name | Result |\n|---|---|\n| Example | PASS |\n\n```json\n{"value":"<script>"}\n```');
 for(const value of ['<ol>','<table>','<th scope="col">','<pre','id="inputs"','&lt;script&gt;'])assert.ok(html.includes(value));
 assert.doesNotMatch(html,/\{#inputs\}|```json|<script>/);
});
test('headings have stable IDs and legacy aliases; code is not a heading',()=>{
 const headings=documentHeadings('## 이름 변경 {#inputs}\n```text\n## Not a heading\n```');assert.equal(headings.length,1);assert.equal(headings[0].id,'inputs');assert.ok(headings[0].aliases.includes(encodeURIComponent('적용 조건')));
});
test('internal links keep policy version and do not leave policy root',()=>{
 assert.equal(policyDocLink('../troubleshooting.md#restart',context),'#/policy/1.0.0/troubleshooting?section=restart');
 for(const href of ['../../secret.md','/secret.md','javascript:alert(1)','data:text/html,x','//evil.example','https://name:secret@example.com'])assert.equal(policyDocLink(href,context),null);
});
test('raw HTML and executable links are not rendered as markup',()=>{
 const html=render('<img src=x onerror=alert(1)>\n\n[bad](javascript:alert)\n\n[guide](../flow.md#pipeline)');assert.doesNotMatch(html,/<img|href="javascript:|onerror="/);assert.match(html,/&lt;img/);assert.match(html,/section=pipeline/);
});
test('implementation references are collapsed separately',()=>{
 const html=render('## 구현·검증 근거 {#references}\n\n- `tests.example.Case.test_rule`');assert.match(html,/<details[^>]+id="references"/);assert.doesNotMatch(html,/<details[^>]+open/);assert.match(html,/<summary>구현·검증 근거<\/summary>/);
});
test('relative rule link resolves within selected documentation version',()=>{
 assert.equal(policyDocLink('rules/P-004.md#remedy',{...context,slug:'troubleshooting'}),'#/policy/1.0.0/rules/P-004?section=remedy');
});

test('old document links normalize to their policy version and keep the section',()=>{
 assert.equal(policyVersionRoute('/policy/1.0.0/rules/I-000?revision=3&section=contract'),'/policy/1.0.0/rules/I-000?section=contract');
 assert.equal(policyVersionRoute('/policy/1.0.0/overview?revision=4'),'/policy/1.0.0/overview');
 assert.equal(policyVersionRoute('/policy/1.0.0/overview'),'/policy/1.0.0/overview');
 assert.equal(policyDocLink('../flow.md',context),'#/policy/1.0.0/flow');
});
