const labels = {PASS: '통과', BLOCK: '차단', UNSUPPORTED: '미지원', ERROR: '검사 오류'};
const stages = {source: '지원 소스', intent: '분석 근거', plan: '배포 설계', patch: '코드 변경', build: '빌드 전 재검사'};

export default function PolicyCard({data, error}) {
  const rows = data?.results ?? [];
  return <section className="card policy-card" aria-label="정책 검사 결과">
    <h2>정책 검사</h2>
    <p className="dim">정책 통과와 실제 배포·공개 접속 확인은 별도 결과입니다.</p>
    {error ? <p role="alert">정책 결과를 불러오지 못했습니다. 잠시 후 다시 확인합니다.</p>
      : !rows.length ? <p>아직 기록된 정책 검사 결과가 없습니다.</p>
      : <ul className="policy-results">{rows.map(row => <li key={row.seq}>
        <div><strong>{row.target.toUpperCase()} · {row.attempt ? '재시도' : '최초 검사'} · {stages[row.stage] || '정책 검사'}</strong>
          <span className={`policy-decision policy-${row.decision.toLowerCase()}`}>{labels[row.decision] || '알 수 없음'}</span></div>
        <p>{row.rule_id} · {row.title}</p>
        {row.path && <p><code>{row.path}{row.line ? `:${row.line}` : ''}</code></p>}
        {row.remedy && <p>다음 조치: {row.remedy}</p>}
        <small className="dim">정책 {row.version} · {row.reason_code}{!row.complete && ' · 검사 미완료'}</small>
      </li>)}</ul>}
  </section>;
}
