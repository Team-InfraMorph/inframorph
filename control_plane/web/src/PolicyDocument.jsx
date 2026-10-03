// Deliberately small, non-executable Markdown renderer for repository policy docs.
// Source HTML is plain text. Relative links remain in the selected policy version.
const legacyAliases = {purpose:['목적과 검사'],inputs:['적용 조건'],cases:['정상 사례','위반·미지원 사례'],remedy:['수정 방법'],limits:['한계'],references:['이력과 검증']};
export function parseHeading(line) {
  const match=/^(#{1,3})\s+(.+?)(?:\s+\{#([a-z][a-z0-9-]*)\})?$/.exec(line);
  return match ? {level:match[1].length,title:match[2],id:match[3]||encodeURIComponent(match[2]),aliases:[encodeURIComponent(match[2]),...(legacyAliases[match[3]]||[]).map(encodeURIComponent)].filter(v=>v!==match[3])} : null;
}
export function documentHeadings(body) {
  let fenced=false;const result=[];
  for(const line of body.split('\n')) {if(line.startsWith('```')){fenced=!fenced;continue;}const heading=!fenced&&parseHeading(line);if(heading)result.push(heading);}
  return result;
}
export function policyVersionRoute(route) {
  const [path,search='']=route.split('?');
  const params=new URLSearchParams(search);
  params.delete('revision');
  return path+(params.size?'?'+params:'');
}
export function policyDocLink(href,{version,slug}) {
  if(/^https:\/\//.test(href)) {try {const u=new URL(href);return u.protocol==='https:'&&!u.username&&!u.password?u.href:null;}catch{return null;}}
  if(/[\\\u0000-\u0020]/.test(href)||href.startsWith('/')||/^[a-z][a-z0-9+.-]*:/i.test(href))return null;
  const [path,fragment='']=href.split('#');
  if(href.split('#').length>2||(!path&&!fragment)||path.includes('?'))return null;
  let destination=slug;
  if(path) {
    if(!path.endsWith('.md'))return null;
    const parts=slug.split('/').slice(0,-1);
    for(const part of path.slice(0,-3).split('/')) {if(part==='..'){if(!parts.length)return null;parts.pop();}else if(part!=='.'){if(!/^[A-Za-z0-9-]+$/.test(part))return null;parts.push(part);}}
    destination=parts.join('/');
  }
  const params=new URLSearchParams();if(fragment)params.set('section',fragment);
  return `#/policy/${encodeURIComponent(version)}/${destination}${params.size?'?'+params:''}`;
}
function Inline({text,context}) {
  return text.split(/(`[^`]+`|\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*)/g).map((part,i)=>{
    if(part.startsWith('`')&&part.endsWith('`'))return <code key={i}>{part.slice(1,-1)}</code>;
    const link=/^\[([^\]]+)\]\(([^)]+)\)$/.exec(part);
    if(link){const href=policyDocLink(link[2],context);return href?<a key={i} href={href} {...(href.startsWith('https:')?{target:'_blank',rel:'noreferrer'}:{})}>{link[1]}{href.startsWith('https:')?' ↗':''}</a>:<span key={i}>{part}</span>;}
    if(part.startsWith('**'))return <strong key={i}>{part.slice(2,-2)}</strong>;
    return <span key={i}>{part.split(/(\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b)/g).map((segment,j)=>segment.includes('_')?<span key={j}>{segment.split('_').map((word,k)=><span key={k}>{k>0&&<wbr/>}{word}{k<segment.split('_').length-1?'_':''}</span>)}</span>:segment)}</span>;
  });
}
const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(s=>s.trim());
const separator=line=>/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line||'');
function Blocks({lines,context}) {
  const rendered=[];
  for(let i=0;i<lines.length;){
    const line=lines[i];if(!line.trim()){i++;continue;}
    const key=i,heading=parseHeading(line);
    if(heading){
      const H=`h${heading.level+1}`;
      const aliases=[...new Set(heading.aliases)].map(id=><span className="policy-anchor" key={id} id={id}/>);
      if(heading.id==='references'){
        const body=[];i++;while(i<lines.length&&!/^#{1,2} /.test(lines[i]))body.push(lines[i++]);
        rendered.push(<details className="policy-doc-references" key={key} id={heading.id}>{aliases}<summary>{heading.title}</summary><Blocks lines={body} context={context}/></details>);
      } else {rendered.push(<H id={heading.id} key={key}>{aliases}{heading.title}</H>);i++;}continue;
    }
    if(line.startsWith('```')){const language=line.slice(3).trim();const content=[];i++;while(i<lines.length&&!lines[i].startsWith('```'))content.push(lines[i++]);if(i<lines.length)i++;rendered.push(<figure className="policy-code" key={key}>{language&&<figcaption>{language}</figcaption>}<pre tabIndex={0}><code>{content.join('\n')}</code></pre></figure>);continue;}
    if(line.trim().startsWith('|')&&separator(lines[i+1])){const head=cells(line),rows=[];i+=2;while(i<lines.length&&lines[i].trim().startsWith('|'))rows.push(cells(lines[i++]));rendered.push(<div className="policy-doc-table" role="region" aria-label="정책 설명 표" tabIndex={0} key={key}><table><thead><tr>{head.map((c,j)=><th scope="col" key={j}><Inline text={c} context={context}/></th>)}</tr></thead><tbody>{rows.map((row,j)=><tr key={j}>{row.map((c,k)=><td key={k}><Inline text={c} context={context}/></td>)}</tr>)}</tbody></table></div>);continue;}
    const list=/^\s*(?:[-*] |\d+\. )/.test(line);
    if(list){const ordered=/^\s*\d+\. /.test(line),List=ordered?'ol':'ul',rows=[];const pattern=ordered?/^\s*\d+\. (.*)$/:/^\s*[-*] (.*)$/;while(i<lines.length&&pattern.test(lines[i]))rows.push(lines[i++].replace(pattern,'$1'));rendered.push(<List key={key}>{rows.map((t,j)=><li key={j}><Inline text={t} context={context}/></li>)}</List>);continue;}
    const paragraph=[line];i++;while(i<lines.length&&lines[i].trim()&&!parseHeading(lines[i])&&!lines[i].startsWith('```')&&!/^\s*(?:[-*] |\d+\. |\|)/.test(lines[i]))paragraph.push(lines[i++]);
    rendered.push(<p key={key}><Inline text={paragraph.join('\n')} context={context}/></p>);
  }
  return rendered;
}
export default function PolicyDocument({body,version,slug}) {
  return <div className="policy-prose" data-rule={slug.startsWith('rules/')}><Blocks lines={body.split('\n')} context={{version,slug}}/></div>;
}
export function documentGroup(slug) {
  if(slug.startsWith('rules/'))return '규칙별 설명';
  if(['overview','responsibilities','results'].includes(slug))return '정책 이해';
  if(['flow','troubleshooting','logs'].includes(slug))return '실행과 문제 해결';
  if(['cases','validation','verification'].includes(slug))return '사례와 검증';
  return '변경과 보완';
}
