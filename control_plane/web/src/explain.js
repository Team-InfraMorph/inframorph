// 모듈이 남긴 실패 문구를 "무슨 일인지 + 다음에 할 일"로 바꾼다. 모르는 문구는 원문만 보여 준다.
// 오류 코드 출처: E policy_gate/gate.py, C analyzer·code_patch, A adapters/aws, 조종실 orchestrator.

const CODES = {
  plan_config_mismatch: ["분석 결과의 환경설정이 배포 설계에 반영되지 않았습니다", "Planner가 검증된 PORT 등 설정을 보존하는지 확인하세요."],
  policy_gate_failed: ["정책 검사를 완료하지 못했습니다", "정책 검사 결과에서 실패한 규칙과 단계를 확인하세요."],
  // E Intent Gate: AI 판단의 근거 검사
  evidence_file_missing: ["AI가 근거로 든 파일이 실제 코드에 없습니다", "AI 판단을 믿을 수 없어 멈췄습니다. 분석을 다시 실행하세요."],
  evidence_line_missing: ["AI가 근거로 든 줄 번호가 실제 파일에 없습니다", "AI 판단을 믿을 수 없어 멈췄습니다. 분석을 다시 실행하세요."],
  unresolved_intent: ["AI가 확신하지 못한 항목이 남아 있습니다", "추측으로 배포하지 않습니다. 코드를 확인하고 다시 실행하세요."],
  revision_mismatch: ["분석한 커밋과 배포할 커밋이 다릅니다", "같은 커밋으로 다시 배포하세요."],
  revision_invalid: ["커밋 SHA 형식이 잘못됐습니다", null],
  schema_invalid: ["AI 분석 결과가 약속된 형식이 아닙니다", "분석을 다시 실행하세요."],
  // E Patch Gate: 고친 코드 검사
  patch_allowlist_violation: ["허용되지 않은 파일을 고치려 했습니다", "DB·저장소 관련 5개 파일만 고칠 수 있습니다. 빌드 전에 막았습니다."],
  deletion_forbidden: ["파일을 지우려 했습니다", "삭제는 허용하지 않습니다. 빌드 전에 막았습니다."],
  forbidden_code_pattern: ["위험한 코드가 들어 있습니다 (셸 실행·동적 코드)", "빌드 전에 막았습니다."],
  secret_in_source: ["코드에 비밀값(키·토큰)이 들어 있습니다", "비밀값은 코드가 아니라 배포 때 주입해야 합니다."],
  artifact_digest_mismatch: ["고친 코드의 지문이 기록과 다릅니다 (위변조 의심)", "빌드 전에 막았습니다."],
  diff_source_mismatch: ["변경 내역과 실제 고친 코드가 다릅니다", "빌드 전에 막았습니다."],
  diff_apply_failed: ["변경 내역을 원본에 적용할 수 없습니다", null],
  change_manifest_mismatch: ["신고된 변경 목록이 실제 변경과 다릅니다", "빌드 전에 막았습니다."],
  javascript_syntax_invalid: ["고친 코드에 문법 오류가 있습니다", null],
  symlink_forbidden: ["심볼릭 링크가 들어 있습니다", "레포 밖 파일을 가리킬 수 있어 거부했습니다."],
  sensitive_source_path: ["민감한 파일(.env 등)이 들어 있습니다", null],
  source_limit: ["파일이 너무 크거나 많습니다", null],
  binary_source_unsupported: ["바이너리 파일은 지원하지 않습니다", null],
  // C Analyzer · Code Patch, 조종실
  analyzer_timeout: ["AI 분석이 시간 안에 끝나지 않았습니다", "잠시 후 다시 배포하세요."],
  analyzer_bad_output: ["AI 분석 결과를 읽을 수 없습니다", "분석을 다시 실행하세요."],
  intent_gate_timeout: ["근거 검사가 시간 안에 끝나지 않았습니다", null],
  patch_timeout: ["코드 수정이 시간 안에 끝나지 않았습니다", null],
  // #30 AWS 배포 경로: 상세 원인은 화면에 내보내지 않고 조종실 서버의 비공개 기록에 남긴다
  aws_pipeline_failed: ["AWS 배포 단계를 완료하지 못했습니다", "원인은 보안상 화면에 표시하지 않습니다. 조종실 서버의 배포 작업 폴더에 있는 failure.json을 확인하세요."],
  aws_adapter_failed: ["AWS 배포 단계를 완료하지 못했습니다", "원인은 보안상 화면에 표시하지 않습니다. 조종실 서버의 배포 작업 폴더에 있는 failure.json을 확인하세요."],
  local_pipeline_failed: ["Local 배포 단계를 완료하지 못했습니다", null],
  mapper_planner_not_connected: ["레포 지도·배포 설계(B)가 연결되지 않았습니다", "데모 모드(control_plane.runtime --demo)로 실행하거나 B Mapper/Planner 명령을 연결하세요."],
  runtime_source_changed: ["분석한 뒤에 소스가 바뀌었습니다", "같은 소스로 다시 분석하고 배포하세요."],
  // E Builder · Local Adapter
  image_tag_collision: ["같은 이름의 이미지가 이미 있습니다", null],
  state_app_mismatch: ["배포 상태 폴더가 앱 이름과 맞지 않습니다", null],
};

const PATTERNS = [
  [/arguments are required: (.+)/, (m) => [
    `AWS 설정값이 없습니다 (${m[1].split(", ").map((a) => AWS_ARGS[a] ?? a).join(", ")})`,
    "조종실을 띄운 터미널에서 .env를 불러온 뒤(set -a; . ./.env; set +a) 조종실을 다시 시작하세요.",
  ]],
  [/Unable to locate credentials|NoCredentials|ExpiredToken|InvalidClientTokenId|security token/i, () => [
    "AWS에 로그인되어 있지 않거나 키가 만료됐습니다", "aws sts get-caller-identity --profile <프로필>로 로그인 상태를 확인하세요.",
  ]],
  [/AccessDenied|not authorized/i, () => ["AWS 권한이 부족합니다", "배포용 IAM 사용자(deployer) 프로필인지 확인하세요."]],
  [/another deployment for this app is already running/, () => [
    "같은 앱의 AWS 배포가 이미 진행 중입니다", "앞 배포가 끝난 뒤 다시 실행하세요.",
  ]],
  [/services are running .* but no deployment record/, () => [
    "AWS에 이 앱이 이미 실행 중인데, 이 조종실은 그 앱을 배포한 기록이 없습니다",
    "다른 노트북(팀원)이 배포한 앱으로 보입니다. 기록 없이 덮어쓰면 실행 중인 앱을 깨뜨릴 수 있어 아무것도 바꾸지 않고 멈췄습니다. 그 배포의 기록 파일(aws-deployment.json)을 상태 폴더로 받아 온 뒤 다시 배포하세요.",
  ]],
  [/deployment record exists but Terraform state .* is missing/, () => [
    "배포 기록은 있는데 AWS 쪽 상태가 없습니다", "실행 중인 앱을 새로 만들지 않도록 멈췄습니다. AWS는 바꾸지 않았습니다.",
  ]],
  [/Error acquiring the state lock|state lock/i, () => [
    "다른 사람이 같은 앱을 AWS에 배포하는 중입니다", "앞 배포가 끝난 뒤 다시 실행하세요.",
  ]],
  [/target group did not become healthy|did not stabilize/, () => [
    "AWS에서 앱이 정상 상태가 되지 않았습니다", "앱 로그(CloudWatch)를 확인하세요.",
  ]],
  [/external HTTPS smoke failed/, () => ["AWS 주소로 접속이 되지 않습니다", "DNS·인증서 전파를 기다린 뒤 '다시 확인'을 누르세요."]],
  [/migration compatibility was not approved/, () => [
    "DB 구조가 바뀐 배포라 자동 롤백을 하지 않았습니다", "옛 코드가 새 DB에서 데이터를 깨뜨릴 수 있어 사람이 판단해야 합니다.",
  ]],
  [/Cannot connect to the Docker daemon|docker.*not running/i, () => ["Docker가 꺼져 있습니다", "Docker Desktop을 켜고 다시 배포하세요."]],
  [/timed out|시간 초과/i, () => ["시간 안에 끝나지 않았습니다", null]],
];

const AWS_ARGS = { "--foundation": "Foundation 출력 파일", "--account-id": "계정 ID", "--state-bucket": "상태 버킷" };

export function explain(detail) {
  if (!detail) return null;
  const code = detail.match(/\b([a-z]+(?:_[a-z]+)+)\b/g)?.find((c) => CODES[c]);
  if (code) return { what: CODES[code][0], next: CODES[code][1] };
  for (const [pattern, build] of PATTERNS) {
    const m = detail.match(pattern);
    if (m) {
      const [what, next] = build(m);
      return { what, next };
    }
  }
  return null;
}

// 배포기·조종실 이벤트의 detail(문구 또는 JSON)을 타임라인용 한 줄로 바꾼다. (#30 C 작성, App.jsx에서 옮김)
export function describe(detail) {
  const messages = {
    image_build_waiting: "다른 배포에서 같은 이미지를 준비하고 있어 완료를 기다립니다.",
    image_build_wait_timeout: "다른 배포의 이미지 빌드가 오래 걸려 대기 시간을 초과했습니다. 완료 후 다시 배포해 주세요.",
    image_build_already_running: "다른 배포가 같은 이미지를 빌드하는 중입니다. 완료 후 다시 배포해 주세요.",
    image_tag_collision: "같은 커밋의 이미지와 수정된 코드가 달라 이미지 검증에 실패했습니다.",
    image_platform_mismatch: "이미지가 배포에 필요한 플랫폼과 일치하지 않습니다.",
    image_provenance_mismatch: "이미지가 승인된 코드로 만들어졌는지 확인하지 못했습니다.",
    untrusted_build_lock: "이미지 빌드 잠금 파일의 안전성을 확인하지 못했습니다.",
    command_failed: "Docker 명령이 실패했습니다. Docker 실행 상태를 확인해 주세요.",
    command_unavailable_or_timeout: "Docker 명령을 실행하지 못했거나 실행 시간을 초과했습니다.",
    local_pipeline_failed: "Local 배포 단계를 완료하지 못했습니다.",
    aws_intent_source_approved: "앱 소스와 분석 결과를 확인했습니다.",
    aws_patch_started: "AWS에 맞게 DB와 저장소 코드를 수정합니다.",
    aws_patch_approved: "AWS 코드 수정의 정책 검사를 통과했습니다.",
    aws_adapter_failed: "AWS 배포 단계를 완료하지 못했습니다. 비공개 진단 기록을 확인해 주세요.",
    "validating exact local linux/amd64 image": "승인된 이미지를 확인합니다.",
    "publishing immutable ECR tag": "승인된 이미지를 ECR에 업로드합니다.",
    "activating digest-pinned ECS services": "ECS 서비스를 실행합니다.",
    "waiting for ALB target health": "로드밸런서에서 앱 상태를 확인합니다.",
    "all registered public targets are healthy": "로드밸런서 상태 검사를 통과했습니다.",
    "performing verified external HTTPS health request": "외부 HTTPS 접속을 확인합니다.",
  };
  if (messages[detail]) return messages[detail];
  try {
    const value = JSON.parse(detail);
    if (messages[value.code]) return messages[value.code];
    if (value.phase === "recoverable_failure") return "자동 테스트 실패를 확인해 한 번 재분석합니다.";
    if (value.code === "retry_recovered") return "재시도 검증을 통과했습니다.";
    if (value.code === "second_local_failure") return "재시도 후에도 실패해 자동 복구를 중단했습니다.";
    if (value.retry_attempt != null) return `자동 복구 ${value.retry_attempt}/1회`;
    if (typeof value.code === "string" && value.code.startsWith("initial_")) return "첫 배포 검증";
    const changes = value.changes ?? value.stage_plan;
    if (changes) return `앱 리소스 추가 ${changes.create} · 수정 ${changes.update} · 삭제 ${changes.delete} · 교체 ${changes.replace}`;
    if (value.mode) return value.mode === "redeploy" ? "기존 앱과 같은 주소로 재배포합니다." : "이 프로젝트 전용 앱을 준비합니다.";
    if (value.services) return `서비스 실행 완료: ${Object.keys(value.services).join(", ")}`;
    if (value.digest) return "이미지를 업로드하고 배포에 사용할 버전을 고정했습니다.";
    if (value.local_image_id) return "승인된 이미지와 커밋을 확인했습니다.";
  } catch { /* E가 보내는 일반 문구도 표시 */ }
  return detail;
}
