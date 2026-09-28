#!/bin/sh
# Выкат ядра на РФ-сервере: только названные сервисы, без сидов и не посреди ИИ-задачи.
#
#   scripts/deploy-core.sh [--force] [--schema | --no-schema] [--ref origin/main] СЕРВИС...
#   СЕРВИС: app scheduler worker playwright-worker
#
# Шаги:
#   1. git fetch + reset --hard на REF — сервер только следует за origin и ничего не
#      сливает; дальше работает уже версия этого скрипта из выкатываемого кода;
#   2. изменился ли schema.sql с последнего успешного выката (коммит в refs/deploy/core,
#      пишется в конце выката) — если да, нужен явный выбор: --schema или --no-schema
#      (без него — отказ, до сборки). Не HEAD до reset: повтор той же команды после отказа
#      или ручной reset перед скриптом видели бы «без изменений» (повторное ревью 23.09).
#      Первый выкат скриптом (ссылки ещё нет) — выбор тоже обязателен;
#   3. сборка названных сервисов — старые контейнеры в это время работают;
#   4. страж: ИИ-задачи в работе (live-ai-leases) — отказ без --force. Итог, пришедший
#      в минуту перезапуска ядра, теряется вместе с оплаченной работой (24.07);
#   5. --schema: init-db без сидов. Это не только CREATE/ALTER: schema.sql идёт одной
#      транзакцией с бэкфиллами (UPDATE articles по всей таблице), а ADD COLUMN IF NOT
#      EXISTS берёт эксклюзивный замок даже на существующую колонку — на время прогона
#      стоят лента (articles) и выдача задач NL (background_jobs). Поэтому только явно и
#      после стража; --no-schema — если новые таблицы и колонки созданы вручную заранее;
#   6. up -d --no-deps только названных — голый `up` 21.09 поднял у агентов лишний
#      планировщик (8,5 ч дублей);
#   7. ожидание health и check-lanes: версии воркеров NL и тревоги сторожа.
#
# Порядок РФ↔NL: сначала ядро (оно понимает и старых воркеров, и новых), потом NL —
# scripts/deploy-nl.sh у владельца.
set -eu

SERVICES_ALLOWED="app scheduler worker playwright-worker"
HEALTH_TIMEOUT=180
# Что выкачено последним успешным прогоном: ссылка в .git, её не трогают ни reset --hard,
# ни git status.
DEPLOY_REF=refs/deploy/core

log() {
  printf '%s deploy-core: %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

die() {
  log "ОТКАЗ: $*"
  exit 1
}

usage() {
  sed -n '2,5p' "$SELF" | sed 's/^# \{0,1\}//'
  exit 64
}

# Команда ядра в свежесобранном образе первого названного сервиса — код уже новый.
run_cli() {
  name="$1"
  shift
  docker compose run --rm --no-deps -T --name "oiltech_deploy_${name}" "$FIRST" \
    python -m oiltech_digest.cli "$@"
}

wait_healthy() {
  svc="$1"
  cid="$(docker compose ps -q "$svc")"
  [ -n "$cid" ] || die "$svc: контейнер не создан"
  waited=0
  while :; do
    state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
    case "$state" in
      "running healthy")
        log "$svc: работает, health ok"
        return 0
        ;;
      "running none")
        # Проверки здоровья у сервиса нет: убедиться, что он не падает сразу после старта.
        sleep 10
        again="$(docker inspect -f '{{.State.Status}} {{.RestartCount}}' "$cid")"
        [ "$again" = "running 0" ] || { docker logs --tail 40 "$cid" 2>&1 || true; die "$svc: после старта $again"; }
        log "$svc: работает"
        return 0
        ;;
      exited* | dead* | *unhealthy)
        docker logs --tail 40 "$cid" 2>&1 || true
        die "$svc: $state"
        ;;
    esac
    if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
      docker logs --tail 40 "$cid" 2>&1 || true
      die "$svc: за ${HEALTH_TIMEOUT} с не поднялся ($state)"
    fi
    sleep 3
    waited=$((waited + 3))
  done
}

main() {
  FORCE=0
  SCHEMA=ask
  REF=origin/main
  UPDATED=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --force) FORCE=1 ;;
      --schema) SCHEMA=schema ;;
      --no-schema) SCHEMA=no-schema ;;
      --ref)
        [ $# -ge 2 ] || usage
        REF="$2"
        shift
        ;;
      --updated) UPDATED=1 ;;  # внутренний: скрипт перезапущен версией из нового кода
      -h | --help) usage ;;
      --*) die "неизвестный флаг $1" ;;
      *) break ;;
    esac
    shift
  done
  [ $# -gt 0 ] || usage
  for svc in "$@"; do
    case " $SERVICES_ALLOWED " in
      *" $svc "*) ;;
      # ${svc} в скобках: bash 3.2 при UTF-8 считает байт «»» частью имени переменной.
      *) die "сервис «${svc}» этим скриптом не выкатывается (можно: $SERVICES_ALLOWED)" ;;
    esac
  done

  cd "$ROOT"
  [ -f .env ] || die "нет .env — скрипт для РФ-ядра"
  [ ! -f .env.external-worker ] || die "здесь .env.external-worker — это NL, для него scripts/deploy-nl.sh"

  if [ "$UPDATED" -eq 0 ]; then
    log "код: $(git rev-parse --short HEAD) → $REF"
    git fetch --quiet origin
    git reset --hard --quiet "$REF"
    flags=""
    [ "$FORCE" -eq 0 ] || flags="$flags --force"
    [ "$SCHEMA" = ask ] || flags="$flags --$SCHEMA"
    # shellcheck disable=SC2086 # flags — список флагов, разбивка по словам нужна
    exec sh "$SELF" --updated $flags --ref "$REF" "$@"
  fi

  GIT_SHA="$(git rev-parse --short HEAD)"
  export GIT_SHA
  FIRST="$1"
  log "выкатываю $GIT_SHA: $*"
  choose="нужен выбор: --schema (init-db целиком: articles и background_jobs под эксклюзивным замком на время прогона, лента и выдача задач NL ждут) или --no-schema (новые таблицы и колонки уже созданы вручную)"
  base="$(git rev-parse -q --verify "${DEPLOY_REF}^{commit}" 2>/dev/null || true)"
  if [ "$SCHEMA" = ask ]; then
    [ -n "$base" ] || die "первый выкат этим скриптом — неизвестно, какая схема уже на базе; $choose"
    git diff --quiet "$base" HEAD -- oiltech_digest/db/schema.sql \
      || die "schema.sql изменился с последнего выката ($(git rev-parse --short "$base")); $choose"
  fi
  free -m 2>/dev/null | sed 's/^/  /' || true

  log "сборка: $*"
  docker compose build "$@"

  if run_cli guard live-ai-leases; then
    :
  else
    code=$?
    if [ "$code" -ne 3 ]; then
      die "страж live-ai-leases не отработал (код $code)"
    elif [ "$FORCE" -eq 1 ]; then
      log "ИИ-задачи в работе, но --force: их итог может потеряться и оплатиться ещё раз"
    else
      die "ИИ-задачи в работе (выше) — дождитесь конца или --force"
    fi
  fi

  if [ "$SCHEMA" = schema ]; then
    log "схема: init-db (без сидов; на время прогона лента и выдача задач NL ждут замка)"
    run_cli schema init-db >/dev/null
  fi

  log "перезапуск: $*"
  docker compose up -d --no-deps "$@"
  for svc in "$@"; do
    wait_healthy "$svc"
  done

  log "сторож полос:"
  run_cli lanes check-lanes || log "у сторожа тревоги (выше); расхождение контракта ожидаемо, пока NL не пересобран"
  # Только после успешного выката: следующий запуск сравнит схему с этим коммитом.
  git update-ref "$DEPLOY_REF" HEAD
  log "готово: $GIT_SHA (записано в $DEPLOY_REF)"
}

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
ROOT="$(cd "$(dirname "$SELF")/.." && pwd)"
# Вызов — последней строкой, после всех определений: git reset --hard меняет этот файл
# под работающим sh, а так он уже прочитан целиком.
main "$@"; exit $?
