# Worklog Bridge (LoroLog)

Worklog Bridge는 Mac에서 수행한 **LoroTOPIK 회사 업무만** 보수적으로 수집·분류해 daily/weekly 근무일지 초안을 만들고, 사람이 검토한 JSON/Markdown을 Windows 회사 PC로 옮겨 HWP 템플릿에 입력하는 로컬 CLI 파이프라인입니다.

이 프로젝트는 자율 보고 봇이 아닙니다. Gmail 전송은 기본적으로 꺼져 있으며, 켜더라도 생성된 파일에서 개인·검토 전용 상세를 제거한 delivery-safe Markdown/JSON 사본만 SMTP SSL로 전달합니다. 운영자는 로컬 원본의 `included_items`, `uncertain_items`, privacy 제외 수와 실제 수신 이메일을 검토해야 합니다.

## 하는 일 / 하지 않는 일

하는 일:

- 지정한 회사 Git repo에서 commit subject, short SHA, 날짜, 변경 파일 **경로**, shortstat만 수집
- 지정한 업무 메모·계획에서 제한된 제목/bullet/짧은 문장만 추출
- `company_work`, `personal_work`, `uncertain`으로 보수적 분류
- daily/weekly JSON + Markdown 생성
- 선택적으로 daily Markdown/JSON을 개인 Gmail 계정으로 전송
- Windows에서 `pyhwpx`, 이후 `win32com` fallback으로 기존 HWP 템플릿 필드 입력

하지 않는 일:

- Git diff/patch, 소스 코드, 파일 본문 전체 수집
- `.env`, API key, token, password, private key, 계정·고객정보, 민감 로그 보존
- 개인 repo commit 조회 또는 개인 프로젝트 내용을 HWP fields에 포함
- Gmail password 저장, OAuth 구현, 외부 파일 동기화
- Mac에서 HWP 생성·수정

## 운영 흐름

```text
Mac config → diagnose → dry-run → launchd/one-shot → JSON+Markdown
                                                     ├─ 선택적 Gmail 전송
                                                     └─ 사람이 검토 → Windows 전달
```

기본 HWP 포함 정책은 `company_work_only`입니다. `uncertain`은 JSON/Markdown 검토 목록에는 남지만 기본 fields에는 들어가지 않습니다. 개인 항목은 상세 내용 없이 프로젝트명 수준의 스텁만 남습니다.

## 1. Mac 설정

요구 환경은 Python 3.10+입니다. Mac collector는 외부 Python 패키지가 필요 없습니다.

이 Mac의 저장소 경로에서 안전한 자동 스캐폴드를 먼저 실행할 수 있습니다. 기존 `config.local.json`은 절대 덮어쓰지 않으며, LoroTOPIK 계열 이름의 실제 Git repo만 제한 탐색합니다.

```bash
cd "/Users/hwangseokbeom/Documents/GitHub/Worklog Bridge"
python3 scripts/setup_macos_local.py
```

스크립트는 `python3.12 → 3.11 → 3.10 → python3` 순으로 탐색하고, outbox/log 생성을 시도합니다. 후보 repo가 정확히 하나일 때만 새 config에 기록합니다. 후보가 없거나 여러 개면 추측하지 않고 exit 2로 남깁니다.

자동 스캐폴드 후에도 config가 없다면 다음 명령으로만 생성합니다. 기존 파일에는 실행하지 않습니다.

```bash
test -e config.local.json || cp config.example.json config.local.json
PYTHON_BIN="$(command -v python3.10)"
"$PYTHON_BIN" --version
```

`config.local.json`에서 예제의 `YOUR_NAME` 경로를 실제 로컬 경로로 바꿉니다. 이 파일은 `.gitignore`에 포함되어 있으므로 commit하지 않습니다.

필수/선택 설정:

- `repo_paths`: LoroTOPIK 회사 repo 절대 경로. 한 개 이상 필요
- `notes.enabled/path`: 메모 수집 여부와 디렉터리
- `plan.enabled/path`: 계획 수집 여부와 파일
- `outbox_dir`: JSON/Markdown 전용 출력 디렉터리
- `log_dir`: launchd stdout/stderr 디렉터리
- `privacy_exclude_patterns`: 기본 차단 규칙에 **추가**할 glob
- `company_keyword_hints`: 사내 repo/업무를 식별할 추가 힌트
- `personal_exclude_hints`: 개인 repo/항목을 제외할 추가 힌트
- `gmail_delivery`: 기본 비활성화된 Gmail 전달 설정. 주소나 App Password 값이 아니라 환경 변수 이름만 저장

기본 `.env`, key, secrets/private/personal 폴더 차단 규칙은 config로 제거할 수 없습니다. config에 token/password 같은 credential 키를 넣으면 로더가 거부합니다.

outbox와 log 디렉터리는 설치 전에 운영자가 직접 만듭니다. source repo 내부, HOME 자체, `.ssh`, `.aws`, `.git`, secrets 경로는 output으로 사용할 수 없습니다.

```bash
mkdir -p "$HOME/Documents/WorklogBridge/outbox"
mkdir -p "$HOME/Documents/WorklogBridge/logs"
CONFIG="$PWD/config.local.json"
```

위 디렉터리와 `CONFIG` 값은 실제 config 내용과 일치해야 합니다.

## 2. Mac 진단

launchd 설치 전 단일 preflight 명령:

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py --config "$CONFIG" --preflight
```

preflight는 Python/config/repo/notes/plan/outbox/log/timezone/privacy와 무파일 read-only 수집을 검사합니다. launchd 상태나 기존 산출물은 판정에 포함하지 않으므로 모든 필수 준비가 끝나면 exit 0이어야 합니다.

launchd 설치 상태와 실제 산출물까지 포함한 진단:

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py --config "$CONFIG" --diagnose
echo $?
```

진단 항목:

- config 존재/스키마/credential-like key 여부
- repo 존재, Git repo 여부, 회사 repo 신호
- 활성화한 notes/plan 경로
- outbox/log 존재 및 쓰기 가능 여부
- source repo 내부 output 차단
- Mac 시스템 시간대 `Asia/Seoul`
- privacy filter 활성 여부
- output을 쓰지 않는 내부 read-only dry-run
- 설치된 plist, 월~금 17:00 스케줄, 현재 config/log 일치 여부
- launchd job load/list 가능 여부와 실제 daily 산출물 존재

종료 코드:

- `0`: READY
- `1`: 경로와 dry-run은 준비됐지만 launchd 미설치 또는 실제 산출물 미생성 등 경고
- `2`: config/필수 경로/privacy/output/schedule가 막힌 상태

Git 활동 유무만 확인하는 안전 진단(소스 코드, diff, commit 제목은 읽거나 출력하지 않음):

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config "$CONFIG" \
  --activity-diagnose \
  --mode daily
```

이 명령은 설정된 repo, 선택 날짜 범위, repo별 최신 commit의 짧은 SHA/시각, 선택 범위 내 commit 존재 여부를 출력합니다. 범위 내 commit이 없으면 `EXPECTED_ZERO_ACTIVITY`로 표시합니다.

## 3. Mac dry-run

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config "$CONFIG" \
  --mode daily \
  --dry-run
```

기간 지정 주간 dry-run:

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config "$CONFIG" \
  --mode weekly \
  --since 2026-06-29 \
  --until 2026-07-03 \
  --dry-run
```

dry-run은 날짜 범위, 분류별 개수, Git 활동 상태, privacy 제외 수, 예상 JSON/Markdown 경로와 `files_written: false`를 출력합니다. 범위 내 Git commit이 없으면 `EXPECTED_ZERO_ACTIVITY`와 "선택한 수집 기간에 Git 활동이 발견되지 않았습니다"를 명시하며, outbox 디렉터리나 daily/weekly 파일을 생성하지 않습니다.

## 4. Mac one-shot 실제 실행

먼저 dry-run 결과를 확인한 뒤 실행합니다.

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config "$CONFIG" \
  --mode daily
```

주간:

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config "$CONFIG" \
  --mode weekly
```

실제 실행은 config의 `outbox_dir`가 존재하고 쓰기 가능할 때만 그 디렉터리 아래에 파일을 생성합니다. 동시에 `log_dir`에 content-free run summary와 `last_run_summary.json`을 기록합니다. `gmail_delivery.enabled=true`인 daily 실행은 두 output 파일을 먼저 안전하게 쓴 다음에만 전달을 시도합니다.

run summary에는 run ID, mode, date range, config 경로, 검토한 repo 경로, Git 조회 전에 건너뛴 개인 repo와 이유, 생성 output, privacy 제외 수, 최종 상태, 실행 시간만 들어갑니다. commit 제목·메모·코드·diff·secret은 기록하지 않습니다.

## 5. launchd 설치: 월~금 17:00 Asia/Seoul

설치 dry-run은 실제 설치와 동일하게 preflight exit 0, 내부 daily dry-run, 같은 config의 성공한 daily one-shot, 비어 있지 않은 `source_repos_considered`, JSON/Markdown output 증거를 모두 검사합니다. 모든 gate를 통과해도 plist를 복사하거나 job을 load하지 않으며 `NOT INSTALLED`로 종료합니다.

```bash
PYTHON_BIN="$PYTHON_BIN" \
  ./scripts/install_macos_launchd.sh --config "$CONFIG" --dry-run
```

실제 설치는 Python 3.10+, `Asia/Seoul` 시스템 시간대, preflight exit 0, installer 내부 dry-run 통과, 같은 config로 성공한 daily one-shot의 `last_run_summary.json`, 비어 있지 않은 `source_repos_considered`, 존재하는 JSON/Markdown output을 모두 요구합니다. 하나라도 없으면 plist를 복사하거나 load하지 않습니다.

설치된 launchd job은 별도 runner나 shell wrapper를 거치지 않습니다. plist의 `ProgramArguments`가 설치 시 확인한 Python 3.10+ binary와 `mac_collect_lorotopik_worklog.py`, `--config <설정 경로> --mode daily`를 각각 독립 argument로 직접 전달하므로 저장소 경로에 공백이 있어도 그대로 동작합니다.

```bash
PYTHON_BIN="$PYTHON_BIN" \
  ./scripts/install_macos_launchd.sh --config "$CONFIG"
```

Gmail 전달을 launchd에서도 사용할 경우 아래 "Reboot-safe Gmail credentials"의 Keychain 설정을 완료합니다. 설치된 plist와 config에는 Gmail 주소, 수신 주소, App Password를 기록하지 않습니다.

설치 후:

```bash
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py --config "$CONFIG" --diagnose
launchctl print "gui/$UID/com.worklogbridge.lorolog.daily"
launchctl kickstart -k "gui/$UID/com.worklogbridge.lorolog.daily"
tail -f "$HOME/Documents/WorklogBridge/logs/lorolog-daily.log"
tail -f "$HOME/Documents/WorklogBridge/logs/lorolog-daily.error.log"
```

로그 경로가 config와 다르면 config의 `log_dir` 경로를 사용합니다. `StartCalendarInterval`은 월~금 17:00이며 installer는 Mac 시스템 시간대가 `Asia/Seoul`이 아니면 설치를 거부합니다.

해제:

```bash
./scripts/uninstall_macos_launchd.sh
```

해제 스크립트는 outbox/log/config를 삭제하지 않습니다.

## 6. Optional Mac → Gmail delivery

daily 생성이 끝나면 Gmail SMTP SSL(`smtp.gmail.com:465`)로 메일을 보낼 수 있습니다. 제목은 `[Worklog Bridge] Daily Draft YYYY-MM-DD`이며, 본문에는 날짜·활동·요약과 제한된 Markdown이 들어갑니다. `daily_worklog_YYYY-MM-DD.md`와 `.json`도 같은 이름으로 첨부됩니다.

전송 직전에 privacy guard를 다시 적용합니다. 첨부 파일은 디스크 원본을 변경하지 않고 메모리에서 delivery-safe 사본으로 만듭니다. Markdown의 uncertain 상세와 개인 프로젝트명은 제거하고, JSON은 Windows HWP에 필요한 `fields`와 고수준 수집 metadata만 허용합니다. `included_items`, `uncertain_items`, `excluded_personal_items`, 로컬 경로는 이메일에 넣지 않습니다. weekly output에는 적용되지 않습니다.

설정은 다음과 같습니다. Gmail 주소, 수신자, App Password 값은 config에 넣지 않습니다.

```json
"gmail_delivery": {
  "enabled": false,
  "smtp_server": "smtp.gmail.com",
  "smtp_port": 465,
  "sender_email_env": "WORKLOGBRIDGE_GMAIL_ADDRESS",
  "app_password_env": "WORKLOGBRIDGE_GMAIL_APP_PASSWORD",
  "recipient_email_env": "WORKLOGBRIDGE_GMAIL_RECIPIENT",
  "attach_markdown": true,
  "attach_json": true,
  "include_markdown_body": true,
  "max_body_chars": 12000,
  "fail_collection_on_delivery_error": false,
  "dedupe": true
}
```

`max_body_chars`는 본문에 복사되는 Markdown 길이만 제한하며 첨부 파일은 생략하지 않습니다. `fail_collection_on_delivery_error=false`이면 전송 실패를 metadata/stderr에 기록하되 이미 성공한 수집은 exit 0을 유지합니다. `true`이면 파일과 실패 기록을 보존한 뒤 collector가 nonzero를 반환합니다.

### Google App Password 준비

1. 발신 Gmail Google Account에서 2-Step Verification을 켭니다.
2. [Google 공식 App Password 안내](https://support.google.com/mail/answer/185833)에 따라 Worklog Bridge 전용 App Password를 만듭니다. 조직·보안 정책 또는 Advanced Protection 때문에 App Password 메뉴가 보이지 않을 수 있습니다.
3. 일반 Gmail password나 회사 계정 password를 사용하지 않습니다. 이 구현은 OAuth를 사용하지 않습니다.

Gmail SMTP 서버, SSL, port 465 외의 SMTP 목적지는 config 검증에서 거부됩니다. Gmail 주소와 수신 주소는 같아도 되며, Google Account의 SMTP 로그인이 허용되어 있어야 합니다.

### 환경 변수, dry-run, 수동 전송

interactive zsh에서는 App Password가 terminal과 history에 나타나지 않도록 silent prompt를 사용합니다. 다음 값은 현재 shell에만 존재하며 `.env`, config, source, script에 저장하지 않습니다.

```zsh
export WORKLOGBRIDGE_GMAIL_ADDRESS="your-account@gmail.com"
export WORKLOGBRIDGE_GMAIL_RECIPIENT="your-personal-address@gmail.com"
read -s "WORKLOGBRIDGE_GMAIL_APP_PASSWORD?Google App Password: "
print
export WORKLOGBRIDGE_GMAIL_APP_PASSWORD
```

dry-run은 파일 resolution, config, privacy-safe payload, 두 attachment, SHA-256 dedupe 상태를 검증합니다. SMTP에 연결하지 않고 메일을 보내지 않으며 delivery metadata도 만들지 않습니다.

```bash
"$PYTHON_BIN" scripts/send_daily_worklog_email.py \
  --config config.local.json --latest --dry-run

"$PYTHON_BIN" scripts/send_daily_worklog_email.py \
  --config config.local.json --date 2026-07-03 --dry-run
```

수동 one-shot은 명시적인 명령이므로 `enabled=false`여도 동작합니다. 먼저 dry-run 결과를 확인합니다.

```bash
"$PYTHON_BIN" scripts/send_daily_worklog_email.py \
  --config config.local.json --latest
```

### Reboot-safe Gmail credentials

`gmail_delivery.enabled=true`이면 daily JSON/Markdown 생성 후 Gmail을 시도하고, `false`이면 기존 collection만 수행합니다. credential resolution 순서는 각 값마다 **명시적 환경 변수 → macOS Keychain → sanitized missing-credential error**입니다. 세 값이 모두 environment이면 `env`, 모두 Keychain이면 `keychain`, 두 위치를 함께 쓰면 `env+keychain`으로만 기록합니다. 실제 값은 출력하지 않습니다.

Keychain은 generic-password service `com.worklogbridge.gmail` 아래에 다음 stable account 이름으로 저장합니다.

- `WORKLOGBRIDGE_GMAIL_ADDRESS`
- `WORKLOGBRIDGE_GMAIL_RECIPIENT`
- `WORKLOGBRIDGE_GMAIL_APP_PASSWORD`

터미널에 노출된 기존 App Password는 새 credential을 저장하기 전에 반드시 폐기합니다. 아래 명령으로 Google App Password 관리 화면을 열고, 노출된 항목의 **Remove**를 누른 뒤 `Worklog Bridge`용 새 App Password를 생성합니다. Google은 App Password를 한 번만 보여 주므로 다른 파일에 복사하지 않습니다.

```zsh
open "https://myaccount.google.com/apppasswords"
```

새 Gmail 주소, 수신 주소, 새 App Password를 Keychain에 저장합니다. `/usr/bin/security`가 각 값을 hidden prompt로 받으며 `-w VALUE` 형태를 사용하지 않으므로 password가 shell history나 process argument에 들어가지 않습니다.

```zsh
cd "/Users/hwangseokbeom/Documents/GitHub/Worklog Bridge"
PYTHON_BIN=/opt/homebrew/bin/python3.10
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config config.local.json --setup-gmail-keychain
unset WORKLOGBRIDGE_GMAIL_ADDRESS WORKLOGBRIDGE_GMAIL_RECIPIENT \
  WORKLOGBRIDGE_GMAIL_APP_PASSWORD
launchctl unsetenv WORKLOGBRIDGE_GMAIL_ADDRESS
launchctl unsetenv WORKLOGBRIDGE_GMAIL_RECIPIENT
launchctl unsetenv WORKLOGBRIDGE_GMAIL_APP_PASSWORD
```

마지막 네 줄은 현재 shell과 GUI launchd session의 임시 override를 제거해 다음 실행이 새 Keychain 값을 사용하게 합니다. 환경 변수가 남아 있으면 resolution order에 따라 Keychain보다 우선합니다.

preflight/diagnose는 Gmail disabled, env available, Keychain available, env+keychain available, credentials missing을 구분합니다. secret 값은 출력하지 않습니다.

```zsh
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config config.local.json --preflight
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config config.local.json --diagnose
```

Keychain 설정 후 기존 direct-Python architecture 그대로 launchd를 재설치하고 kickstart합니다.

```zsh
PYTHON_BIN="$PYTHON_BIN" ./scripts/install_macos_launchd.sh \
  --config config.local.json
launchctl kickstart -k "gui/$UID/com.worklogbridge.lorolog.daily"
launchctl print "gui/$UID/com.worklogbridge.lorolog.daily" | \
  sed -n '/last exit code/p'
```

configured `log_dir`의 sanitized metadata와 Gmail 받은편지함을 모두 확인합니다. `SKIPPED_DUPLICATE`이면 동일 Markdown hash가 이미 성공한 것이므로 새 SMTP 전송 증거가 아닙니다.

```zsh
sed -n '1,160p' \
  "$HOME/Documents/WorklogBridge/logs/gmail_delivery_$(date +%F).json"
open "https://mail.google.com/"
```

metadata에는 credential source만 들어가며 address, recipient, App Password는 들어가지 않습니다. 받은편지함에서 `[Worklog Bridge] Daily Draft YYYY-MM-DD` 제목과 JSON/Markdown 첨부를 직접 확인하기 전에는 live delivery가 검증된 것이 아닙니다.

환경 변수를 이용한 non-interactive Keychain setup 검사는 tests/dry-run 전용이며 Keychain을 변경하지 않습니다.

```zsh
"$PYTHON_BIN" mac_collect_lorotopik_worklog.py \
  --config config.local.json --setup-gmail-keychain --dry-run
```

### 임시 session 환경 변수와 launchd

`launchctl setenv`도 임시 override로 계속 지원합니다. launchd는 interactive shell의 `export`를 자동 상속하지 않으므로 현재 GUI launchd session에 다음처럼 전달합니다. 이 방식은 logout/reboot 후 사라질 수 있으므로 운영에는 Keychain을 권장합니다.

```zsh
read -s "WORKLOGBRIDGE_GMAIL_ADDRESS?Gmail address (hidden): "
print
read -s "WORKLOGBRIDGE_GMAIL_RECIPIENT?Recipient address (hidden): "
print
read -s "WORKLOGBRIDGE_GMAIL_APP_PASSWORD?Google App Password: "
print
launchctl setenv WORKLOGBRIDGE_GMAIL_ADDRESS "$WORKLOGBRIDGE_GMAIL_ADDRESS"
launchctl setenv WORKLOGBRIDGE_GMAIL_APP_PASSWORD "$WORKLOGBRIDGE_GMAIL_APP_PASSWORD"
launchctl setenv WORKLOGBRIDGE_GMAIL_RECIPIENT "$WORKLOGBRIDGE_GMAIL_RECIPIENT"
unset WORKLOGBRIDGE_GMAIL_ADDRESS WORKLOGBRIDGE_GMAIL_RECIPIENT \
  WORKLOGBRIDGE_GMAIL_APP_PASSWORD
launchctl kickstart -k "gui/$UID/com.worklogbridge.lorolog.daily"
```

`launchctl setenv`는 현재 로그인한 user launchd session에만 적용되며 reboot-safe credential 저장 방식이 아닙니다. login/reboot로 GUI launchd session이 새로 시작되면 위 runtime 환경 변수를 다시 설정해야 합니다. 제거할 때는 `launchctl unsetenv`를 각 변수에 실행합니다. 설치된 plist에는 Gmail 값이 없고 Python collector는 resolved 값을 출력하지 않습니다.

설치된 plist와 config에는 Gmail credential이 없으며 collector가 실행 시 Keychain을 직접 조회합니다. login Keychain은 user login 뒤 unlocked 상태여야 합니다.

### Dedupe, 재전송, 실패 처리

`dedupe=true`이면 원본 Markdown SHA-256 hash가 같은 성공 전송은 `SKIPPED_DUPLICATE`로 건너뜁니다. 내용이 바뀌면 다시 전송하고, 의도적인 동일 내용 재전송만 `--force`를 사용합니다.

```bash
"$PYTHON_BIN" scripts/send_daily_worklog_email.py \
  --config config.local.json --latest --force
```

날짜별 상태는 `log_dir/gmail_delivery_YYYY-MM-DD.json`에 기록됩니다. date, filename, hash, status, 제한된 SMTP response, sanitized error, sent_at, credential source만 기록하고 address, recipient, App Password, SMTP AUTH, message body, attachment 본문은 기록하지 않습니다. 실패해도 outbox JSON/Markdown은 삭제하거나 덮어쓰지 않습니다.

실패 시 metadata와 `lorolog-daily.error.log`에서 환경 변수 이름, 인증 실패 여부, SMTP 도달 여부를 확인합니다. 실제 Gmail 받은편지함에 제목과 두 첨부 파일이 도착한 것을 확인하기 전에는 production Gmail delivery가 검증된 것이 아닙니다.

### 매일 검토

1. outbox의 `daily_worklog_YYYY-MM-DD.json`과 `.md`를 엽니다.
2. `included_policy`, `date_range`, `included_items`, `uncertain_items`, `excluded_personal_items`, `privacy_exclusions_summary`를 검토합니다.
3. `uncertain`을 회사 업무라고 자동 가정하지 않습니다. 필요하면 원본 메모를 명확히 고치고 다시 생성합니다.
4. 개인 상세 내용, code/diff, secret-looking 문자열이 없는지 확인합니다.
5. Gmail 수신 message와 delivery-safe 첨부 파일이 의도한 회사 업무만 포함하는지 확인합니다.

## Mac 엄격 체크리스트

1. Python 3.10+ 확인
2. `/Users/hwangseokbeom/Documents/GitHub/Worklog Bridge`에서 실행
3. `config.local.json` 생성/보존 확인
4. source repo 밖 outbox/log 디렉터리 확인
5. 실제 LoroTOPIK repo 및 활성화한 notes/plan 경로 확인
6. `--preflight` exit 0 확인
7. daily `--dry-run`과 `files_written: false` 확인
8. daily one-shot 실제 실행
9. JSON/Markdown 및 safe run summary 수동 검토
10. 위 검증 후에만 launchd 설치
11. `launchctl kickstart` 한 번 실행
12. stdout/stderr log와 outbox 파일 확인
13. Gmail 자동 전달을 켰다면 실제 수신 email과 delivery metadata 확인

## 7. Windows HWP 준비와 dry-run

회사 Windows PC에 Python 3.10+, 한컴오피스 한글을 준비합니다. 허용되는 경우에만 선택 의존성을 설치합니다.

```powershell
py -m pip install pyhwpx pywin32
cd "C:\path\to\Worklog Bridge"
```

JSON 필수 fields와 template/output 경로만 검증하고 한글을 열거나 파일을 쓰지 않는 dry-run:

```powershell
py .\windows_fill_hwp.py `
  --json "$env:USERPROFILE\Downloads\daily_worklog_2026-07-02.json" `
  --template "C:\worklog\template.hwp" `
  --output "C:\worklog\filled\daily_worklog_2026-07-02_filled.hwp" `
  --dry-run
```

`json_valid`, `template_output_distinct`, `hancom_opened: false`, `files_written: false`를 확인합니다.

한컴이 설치된 환경에서 템플릿을 열어 필드 목록만 검사하고 저장하지 않는 모드:

```powershell
py .\windows_fill_hwp.py `
  --template "C:\worklog\template.hwp" `
  --validate-template-fields `
  --visible false
```

필수 필드:

```text
DATE WEEK_RANGE SUMMARY TASKS BUSINESS_ANALYSIS APP_DIRECTION DEV_WORK
LEARNINGS DIFFICULTIES NEXT_PLAN COMMENT
```

필드 검증은 모두 있으면 exit 0, 누락이면 3, `pyhwpx`와 `HWPFrame.HwpObject` 모두 사용할 수 없으면 명확한 오류와 exit 2를 반환합니다. 어떤 output도 쓰지 않습니다.

## 8. Windows HWP 실제 실행

dry-run과 필드 검증 후 실행합니다.

```powershell
py .\windows_fill_hwp.py `
  --json "$env:USERPROFILE\Downloads\daily_worklog_2026-07-02.json" `
  --template "C:\worklog\template.hwp" `
  --output "C:\worklog\filled\daily_worklog_2026-07-02_filled.hwp" `
  --visible true
```

template과 output이 같으면 즉시 거부합니다. 원본 템플릿은 저장하거나 덮어쓰지 않습니다. `pyhwpx` 실패 후 `win32com`도 실패하면 같은 output 위치에 수동 붙여넣기용 `.txt`를 생성하고 exit 2를 반환합니다.
Windows system/credential/source 관련 unsafe output 경로도 거부합니다.

최신 다운로드 JSON 실행:

```powershell
.\scripts\windows_run_latest.ps1 `
  -Template "C:\worklog\template.hwp" `
  -InputDir "$env:USERPROFILE\Downloads" `
  -OutputDir "C:\worklog\filled"
```

회사 정책이 스크립트/COM 자동화를 막으면 정책을 우회하지 말고 Markdown/TXT를 한글 템플릿에 수동으로 붙여넣습니다.

## 9. 출력 스키마와 보안

daily/weekly JSON은 동일한 상위 구조를 사용합니다.

- `date`, `week_id`, `week_range`, `date_range`
- `included_policy`, `fields`
- `collection_summary`: 수집 항목 수, Git 활동 수/상태, 포함된 회사 업무 수, 선택 기간 활동 안내
- `included_items`: source repo/file, 분류, 분류 이유, 짧은 초안
- `excluded_personal_items`: 상세 내용 없이 프로젝트명과 제외 이유
- `uncertain_items`: source, 분류 이유, 짧은 초안 — 기본 fields 제외
- `privacy_exclusions_summary`: 제외 수와 정책
- `privacy_note`

Git command는 `log`, `diff-tree --name-only`, `show --shortstat`만 사용합니다. patch/diff 본문과 파일 내용은 읽지 않습니다. 메모/계획은 허용 확장자의 제한된 라인만 추출하며 code fence/diff/secret 패턴은 버립니다.

## 10. 테스트

공식 pytest가 있는 환경:

```bash
python3 -m pytest -q
```

pytest를 설치할 수 없는 잠금 환경:

```bash
"$PYTHON_BIN" scripts/run_compat_tests.py
```

호환 실행기는 시작 시 `NOT OFFICIAL PYTEST`를 출력합니다. pytest fixture/runner의 전체 동작과 동등하지 않으며 결과를 공식 pytest 성공으로 보고하면 안 됩니다.

추가 운영 검증:

```bash
plutil -lint launchd/com.worklogbridge.lorolog.daily.plist
zsh -n scripts/install_macos_launchd.sh scripts/uninstall_macos_launchd.sh
"$PYTHON_BIN" windows_fill_hwp.py \
  --json examples/sample_daily_worklog.json \
  --template /tmp/template.hwp \
  --output /tmp/filled.hwp \
  --dry-run
```

## 11. 문제 해결

- **diagnose exit 2:** `[BLOCKED]` 줄부터 수정합니다. config 예제 경로를 실제 경로로 바꿨는지 먼저 확인합니다.
- **회사 repo로 확인되지 않음:** 잘못된 repo를 넣지 않았는지 확인하고, 실제 회사 repo라면 `company_keyword_hints`에 명확한 회사 식별어를 추가합니다.
- **outbox/log 없음:** config와 같은 경로를 `mkdir -p`로 만든 후 재진단합니다.
- **output path guard:** source repo 내부가 아닌 전용 `~/Documents/WorklogBridge/outbox` 같은 경로를 사용합니다.
- **launchd 미등록:** installer의 정상 종료와 `launchctl print`를 확인합니다. plist만 있고 load되지 않으면 재설치합니다.
- **17시에 실행되지 않음:** `/etc/localtime`이 `Asia/Seoul`인지, 설치된 plist 스케줄과 log를 확인합니다.
- **Gmail delivery FAILED:** `--preflight`에서 credential source를 확인하고 `gmail_delivery_YYYY-MM-DD.json`의 sanitized status를 봅니다. missing이면 `--setup-gmail-keychain`을 다시 실행합니다. address, recipient, App Password를 terminal/log에 출력해 진단하지 않습니다.
- **Gmail delivery SKIPPED_DUPLICATE:** 같은 날짜와 같은 Markdown hash가 이미 성공한 상태입니다. 의도적 재전송만 `--force`를 사용합니다.
- **결과가 비어 있음:** 수집 기간, commit 날짜, 메모 수정 시간, keyword hint를 확인합니다. 신호가 약하면 의도적으로 `uncertain`입니다.
- **HWPFrame.HwpObject/pyhwpx 오류:** Windows·한글 설치, Python/한글 bitness, 회사 COM 정책을 확인합니다. 우회하지 말고 TXT fallback을 사용합니다.
- **필드 누락:** `--validate-template-fields`의 `missing_required_fields`와 템플릿 필드명 대소문자/밑줄을 비교합니다.

## 12. 상태 의미

- `IMPLEMENTED`: 이 환경에서 실제 config 경로, Mac launchd 설치/17시 실행, 실제 산출물, 전체 테스트, Windows 한글 HWP 입력까지 검증됨
- `PARTIAL`: 코드와 안전장치는 완료했지만 실제 launchd 17시 실행 또는 Windows/HWP 검증 일부가 남음
- `BLOCKED_WITH_REASON`: 실제 config/path, launchd, Windows/한글처럼 필요한 운영 환경이 없어 검증을 진행할 수 없음
- `CONFIG_SCAFFOLDED`: config.local.json과 안전한 outbox/log가 준비됐지만 실제 회사 repo 경로가 아직 없음

코드가 존재한다는 이유만으로 `IMPLEMENTED`라고 보고하지 않습니다.

## Windows Manual Steps

1. Mac outbox의 JSON/Markdown을 사람이 검토합니다.
2. Gmail로 받은 delivery-safe JSON/Markdown 또는 승인된 공유 수단을 사용해 필요한 JSON을 Windows PC로 이동합니다.
3. Windows Python 3.10+를 확인합니다.
4. 한컴오피스 한글과 HWP COM 사용 가능 여부를 확인합니다.
5. 원본 회사 템플릿을 안전한 template 전용 경로에 둡니다.
6. `windows_fill_hwp.py --dry-run`으로 JSON fields와 경로를 검사합니다.
7. `--validate-template-fields`로 실제 템플릿 field mapping을 검사합니다.
8. 필요하면 `--visible true`로 실제 HWP fill을 실행합니다.
9. 생성된 HWP를 사람이 검토한 뒤 제출합니다.

Windows + 한컴오피스 COM에서 실제 실행하기 전에는 HWP 자동화가 검증됐다고 간주하지 않습니다.
