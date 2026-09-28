#!/bin/sh
# Выкат воркеров NL: по одному контейнеру, с мягкой остановкой.
#
#   scripts/deploy-nl.sh [--ref origin/main] [СЕРВИС...]
#   СЕРВИС (по умолчанию все): external-worker external-worker-bulk external-worker-fetch external-worker-browser external-worker-agents
#
# Запускать на NL в каталоге репозитория (/root/oiltech-digest). Шаги:
#   1. git fetch + reset --hard на REF, дальше работает версия скрипта из нового кода;
#   2. сборка всех названных образов, пока старые воркеры работают (SHA коммита — в образ);
#   3. по одному: up -d --no-deps — Docker шлёт SIGTERM, воркер возвращает задачи ядру
#      (release) и выходит в пределах stop_grace_period (120 с); новый контейнер обязан
#      отметиться в ядре новой сборкой (worker-versions --self) — иначе остановка, остальные
#      контейнеры не трогаем;
#   4. в конце — версии всех воркеров глазами ядра.
#
# Не во время ежедневного радара (в едином контуре — первый цикл планировщика после 00:00 МСК; до D — крон 07:15; прогон до ~20 мин): прогон в external-worker-agents по
# частям не сохраняется (signal_discovery не в PARTIAL_KINDS) и начнётся заново — повторные запросы
# к Brave и судье. На ядре: cli external-queues-status (external-agents: running=0). Страж — задача D.
#
# Ядро выкатывается раньше (scripts/deploy-core.sh на РФ): оно понимает и старых, и новых.
set -eu

COMPOSE_FILE=docker-compose.external-worker.yml
ALL_SERVICES="external-worker external-worker-bulk external-worker-fetch external-worker-browser external-worker-agents"
CHECKIN_TIMEOUT="${DEPLOY_NL_CHECKIN_TIMEOUT:-120}"

log() {
  printf '%s deploy-nl: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

die() {
  log "ОТКАЗ: $*"
  exit 1
}

usage() {
  sed -n '2,5p' "$SELF" | sed 's/^# \{0,1\}//'
  exit 64
}

compose() {
  docker compose -f "$COMPOSE_FILE" "$@"
}

# Новый контейнер отметился в ядре: сборка — наша, контракт — как у ядра.
wait_checkin() {
  svc="$1"
  waited=0
  while [ "$waited" -lt "$CHECKIN_TIMEOUT" ]; do
    if compose exec -T "$svc" python -m oiltech_digest.cli worker-versions --self --expect-build "$GIT_SHA" \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
  done
  return 1
}

main() {
  REF=origin/main
  UPDATED=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --ref)
        [ $# -ge 2 ] || usage
        REF="$2"
        shift
        ;;
      --updated) UPDATED=1 ;;
      -h | --help) usage ;;
      --*) die "неизвестный флаг $1" ;;
      *) break ;;
    esac
    shift
  done

  cd "$ROOT"
  # 21.09 NL-команду по ошибке запустили на РФ-ядре; спасло отсутствие этого файла.
  [ -f .env.external-worker ] || die "нет .env.external-worker — это не NL"
  if docker ps --format '{{.Names}}' | grep -qx oiltech_app; then
    die "здесь работает oiltech_app — это РФ-ядро, а не NL"
  fi

  if [ "$UPDATED" -eq 0 ]; then
    log "код: $(git rev-parse --short HEAD) → $REF"
    git fetch --quiet origin
    git reset --hard --quiet "$REF"
    exec sh "$SELF" --updated --ref "$REF" "$@"
  fi

  # shellcheck disable=SC2086 # список сервисов по умолчанию — разбивка по словам нужна
  [ $# -gt 0 ] || set -- $ALL_SERVICES
  for svc in "$@"; do
    case " $ALL_SERVICES " in
      *" $svc "*) ;;
      *) die "нет такого воркера «${svc}» (есть: $ALL_SERVICES)" ;;
    esac
  done

  GIT_SHA="$(git rev-parse --short HEAD)"
  export GIT_SHA
  log "сборка $GIT_SHA: $*"
  compose build "$@"

  for svc in "$@"; do
    # Воркер сборки до мягкой остановки — python под PID 1 без обработчика SIGTERM: сигнал
    # он игнорирует, и Docker ждал бы все 120 с stop_grace_period ради того же SIGKILL.
    # Такого узнаём по отсутствию команды worker-versions и гасим за 10 с.
    if compose exec -T "$svc" python -m oiltech_digest.cli worker-versions --help >/dev/null 2>&1; then
      log "$svc: мягкая остановка (до 120 с: задачи вернутся ядру) и запуск $GIT_SHA"
      compose up -d --no-deps "$svc"
    else
      log "$svc: старая сборка без мягкой остановки — гашу за 10 с, задачи вернутся по аренде; запуск $GIT_SHA"
      compose up -d --no-deps --timeout 10 "$svc"
    fi
    if wait_checkin "$svc"; then
      log "$svc: отметился в ядре сборкой $GIT_SHA"
    else
      compose logs --tail 40 "$svc" || true
      die "$svc не отметился в ядре за ${CHECKIN_TIMEOUT} с — остальные воркеры не трогаю"
    fi
  done

  log "версии воркеров глазами ядра:"
  compose exec -T "$1" python -m oiltech_digest.cli worker-versions
  log "готово: $GIT_SHA"
}

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
# Вызов — последней строкой, после всех определений: git reset --hard меняет этот файл
# под работающим sh, а так он уже прочитан целиком.
main "$@"; exit $?
