// AWS 구성도. Plan에는 '앱이 무엇을 필요로 하나'만 있다(VPC·ALB·ARN 등은 배포기가 정한다, schemas/README.md).
// 그래서 서비스·DB·저장소·비밀값·로그는 Plan에서, 둘레(ALB·VPC·ECR·Secrets Manager·CloudWatch)는
// A의 terraform/foundation(여러 앱 공용) + terraform/app(앱 전용)이 실제로 만드는 구성대로 그린다.

const ICON = {
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-7 8a7 7 0 0 1 14 0",
  gate: "M12 3 4 6v6c0 4.4 3.4 8.3 8 9 4.6-.7 8-4.6 8-9V6l-8-3Zm-3 9 2 2 4-4",
  server: "M4 5h16v5H4zM4 14h16v5H4zM8 7.5h.01M8 16.5h.01",
  worker: "M12 8v4l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z",
  db: "M12 3c4.4 0 8 1.3 8 3s-3.6 3-8 3-8-1.3-8-3 3.6-3 8-3Zm-8 3v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  bucket: "M4 6h16l-2 13H6L4 6Zm0 0c0-1.7 3.6-3 8-3s8 1.3 8 3",
  image: "M3 7l9-4 9 4-9 4-9-4Zm0 0v10l9 4 9-4V7M12 11v10",
  key: "M15 7a4 4 0 1 1-3.9 5H3v3h3v-3",
  logs: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h5",
};

function Icon({ name }) {
  return (
    <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">
      <path d={ICON[name]} fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** 상자 하나 = AWS 부품 하나. 제목(무엇) + 한 줄 설명(왜) + 공용 여부. */
function Part({ icon, title, sub, what, shared, tone }) {
  return (
    <div className={`part${shared ? " shared" : ""}${tone ? ` t-${tone}` : ""}`}>
      <div className="part-head"><Icon name={icon} /><strong>{title}</strong>{shared && <span className="chip">공용</span>}</div>
      {sub && <div className="part-sub mono">{sub}</div>}
      <div className="part-what">{what}</div>
    </div>
  );
}

const Arrow = ({ label }) => <div className="arrow"><span>{label}</span></div>;

export function AwsArchitecture({ plan }) {
  const web = plan.services.filter((s) => s.public);
  const inner = plan.services.filter((s) => !s.public);
  return (
    <div className="arch">
      <div className="arch-flow">
        <Part icon="user" title="사용자" what="브라우저로 접속" />
        <Arrow label="HTTPS" />
        <Part icon="gate" title="로드밸런서 (ALB)" shared what="인터넷 요청을 받는 입구 · HTTPS 인증서로 암호화하고 앱 주소별로 나눠 보냄" />
        <Arrow label="" />
        <div className="vpc">
          <div className="vpc-label">VPC 비공개 구역 · 인터넷에서 직접 못 들어옴</div>
          {web.map((s) => (
            <Part key={s.name} icon="server" tone="blue" title={`${s.name} 서비스`} sub={`ECS Fargate · :${s.port} · ${s.cpu} CPU · ${s.mem}MB`}
                  what="앱(백엔드 서버)이 실행되는 곳 · 서버 관리 없이 컨테이너만 돌림" />
          ))}
          {inner.map((s) => (
            <Part key={s.name} icon="worker" tone="blue" title={`${s.name} (백그라운드)`} sub={`ECS Fargate · ${s.command ?? ""}`}
                  what="밖에서 접속하지 않는 내부 작업" />
          ))}
        </div>
        <Arrow label="" />
        <div className="arch-data">
          {plan.db && <Part icon="db" tone="green" title="DB · RDS PostgreSQL" shared what="데이터 저장 · 공용 DB 서버 안에 이 앱 전용 DB를 따로 만듦" />}
          {plan.storage && <Part icon="bucket" tone="green" title="파일 · S3 버킷" sub={plan.storage.path} what="업로드한 파일 보관 · 앱을 다시 배포해도 남음" />}
        </div>
      </div>
      <div className="arch-support">
        <span className="support-label">배포·운영을 돕는 부품</span>
        <Part icon="image" title="ECR" shared sub={plan.image_tag.slice(0, 11)} what="빌드한 앱 이미지 보관함" />
        {plan.secrets?.length > 0 && <Part icon="key" title="Secrets Manager" sub={plan.secrets.join(", ")} what="DB 비밀번호 같은 비밀값 보관 · 앱 시작 때만 꺼내 줌" />}
        {plan.logs && <Part icon="logs" title="CloudWatch" what="앱 로그 모음" />}
      </div>
    </div>
  );
}
