// 코드 변경 내역: 파일마다 '실제로 바뀐 핵심 줄'을 바로 보여 준다. 요약 문장을 지어내지 않고 diff에서 뽑는다.

const TARGET = { local: "Local", aws: "AWS", gcp: "GCP", onprem: "온프레미스" };

function lines(diff) {
  return (diff ?? "").split("\n").filter((l) => /^[+-]/.test(l) && !/^(\+\+\+|---)/.test(l));
}

/** 대표 줄 고르기: 상투적인 줄('use strict', require/import, 괄호만)은 뒤로, 선언·호출·설정 줄을 앞으로. 원래 순서는 유지. */
function keyLines(diff) {
  const score = (l) => {
    const body = l.slice(1).trim();
    if (body.length <= 2 || /^[{}()[\];,]+$/.test(body) || /^(\/\/|#|\*|\/\*)/.test(body)) return -9;
    let v = 0;
    if (/^["']use strict["']/.test(body) || /\brequire\(|^import\b/.test(body)) v -= 3;
    if (/\b(function|class|async|export|module\.exports)\b|=>|provider|STORAGE|S3|\.send\(|writeFile|readFile|saveImage|readImage|process\.env|env\./.test(body)) v += 3;
    return v;
  };
  const pick = (sign, n) => lines(diff).map((l, i) => ({ l, i, v: score(l) })).filter((x) => x.l[0] === sign && x.v > -9)
    .sort((a, b) => b.v - a.v || a.i - b.i).slice(0, n).sort((a, b) => a.i - b.i).map((x) => x.l);
  return [...pick("-", 2), ...pick("+", 3)];
}

/** GitHub식 변경량 막대: 5칸을 추가·삭제 비율로 칠한다. */
function Bar({ add, del }) {
  const total = add + del;
  const green = total ? Math.round((add / total) * 5) : 0;
  return (
    <span className="pbar" aria-label={`추가 ${add}줄, 삭제 ${del}줄`}>
      {Array.from({ length: 5 }, (_, i) => <i key={i} className={i < green ? "pb-add" : total ? "pb-del" : ""} />)}
    </span>
  );
}

function FileCard({ f }) {
  const changed = lines(f.diff);
  const add = changed.filter((l) => l[0] === "+").length;
  const del = changed.length - add;
  const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/") + 1) : "";
  const name = f.path.slice(dir.length);
  const key = keyLines(f.diff);
  return (
    <div className={`pcard pc-${f.action === "add" ? "add" : "mod"}`}>
      <div className="pc-head">
        <span className={`pc-badge ${f.action === "add" ? "pcb-add" : "pcb-mod"}`}>{f.action === "add" ? "새 파일" : "수정"}</span>
        <span className="pc-path"><span className="dim">{dir}</span><strong>{name}</strong></span>
        {f.diff != null && <span className="pc-stat"><b className="s-add">+{add}</b> <b className="s-del">−{del}</b> <Bar add={add} del={del} /></span>}
      </div>
      {f.diff == null ? (
        <p className="pc-skip">의존성 잠금 파일이라 내용은 생략 (위 package.json 변경에 맞춰 다시 만든 것)</p>
      ) : (
        <pre className="pc-key">
          {key.map((l, i) => <span key={i} className={l[0] === "+" ? "k-add" : "k-del"}>{l}{"\n"}</span>)}
          {changed.length > key.length && <span className="k-more">… 바뀐 줄 {changed.length - key.length}개 더</span>}
        </pre>
      )}
      {f.diff != null && (
        <details className="pc-full">
          <summary>전체 diff 보기</summary>
          <pre className="diff2">
            {f.diff.split("\n").map((l, i) => (
              <span key={i} className={/^@@/.test(l) ? "d-hunk" : l[0] === "+" ? "d-add" : l[0] === "-" ? "d-del" : ""}>{l || " "}{"\n"}</span>
            ))}
          </pre>
          {f.truncated && <p className="dim">표시 크기 제한으로 diff 일부를 생략했습니다.</p>}
        </details>
      )}
    </div>
  );
}

export function PatchCard({ patch = {}, loading = false, error = false }) {
  const entries = Object.entries(patch);
  if (loading || error || !entries.length) return (
    <div className="card patch2" aria-busy={loading}>
      <strong>코드 변경</strong>
      <p role={error ? "alert" : "status"} className="dim">{loading
        ? "코드 변경 내역을 불러오는 중입니다."
        : error ? "코드 변경 내역을 불러오지 못했습니다. 배포 기록을 다시 선택해 주세요."
        : "이 배포에는 아직 저장된 코드 변경 내역이 없습니다. 코드 변경이 없다는 판정과는 다릅니다."}</p>
    </div>
  );
  const key = (p) => JSON.stringify(p.files.map((f) => [f.path, f.action, f.diff]));
  const same = entries.length > 1 && entries.every(([, p]) => key(p) === key(entries[0][1]));
  const groups = same ? [[entries.map(([t]) => TARGET[t] ?? t).join("·") + " 공통", entries[0][1]]] : entries.map(([t, p]) => [TARGET[t] ?? t, p]);
  return groups.map(([label, value]) => {
    // 소스 코드 먼저, 잠금 파일은 맨 뒤
    const files = [...value.files].sort((a, b) => (a.diff == null) - (b.diff == null));
    const all = files.flatMap((f) => lines(f.diff));
    const add = all.filter((l) => l[0] === "+").length;
    return (
      <div key={label} className="card patch2">
        <div className="patch-head">
          <strong>코드 변경 · {label}</strong>
          <span className="dim">파일 {files.length}개 · <b className="s-add">+{add}</b> <b className="s-del">−{all.length - add}</b></span>
          {value.verified && <a className="ok-chip" href="#policy-inspections" onClick={e=>{e.preventDefault();document.getElementById("policy-inspections")?.scrollIntoView();}}>패치 검사 통과 · 상세</a>}
          {value.applied && <span className="ok-chip">실행 검증 완료</span>}
          {value.phase === "recovery" && <span className="dim">자동 복구 후 패치</span>}
          <span className="dim patch-note">원본 레포는 그대로, 배포용 복사본만 수정</span>
        </div>
        {value.status === "unchanged" ? <p className="dim">고칠 코드가 없어 원본 그대로 배포했습니다.</p> : (
          <div className="pgrid">{files.map((f) => <FileCard key={f.path} f={f} />)}</div>
        )}
      </div>
    );
  });
}
