import { Suspense, lazy, useEffect, useState } from "react";
import { listArticles } from "../api/articles";
import { ApiError } from "../api/client";
import { getSession, login, logout, register } from "../api/auth";
import { getDashboardStats } from "../api/stats";
import type { Article, DashboardStats, User } from "../api/types";
import { ArticlesPage, DEFAULT_SIGNAL_ARTICLE_QUERY } from "../features/articles/ArticlesPage";
import { BacklogPage } from "../features/backlog/BacklogPage";
import { DigestPage } from "../features/digest/DigestPage";
import { DocumentsPage } from "../features/documents/DocumentsPage";
import { JobsPage } from "../features/jobs/JobsPage";
import { StatisticsPage } from "../features/statistics/StatisticsPage";
import { MaintenancePage } from "../features/maintenance/MaintenancePage";
import { ScoringPage } from "../features/scoring/ScoringPage";
import { SignalRadarPage } from "../features/signals/SignalRadarPage";
import { SourceAgentPage } from "../features/sources/SourceAgentPage";
import { SourceCandidatesPage } from "../features/sources/SourceCandidatesPage";
import { SourcesPage } from "../features/sources/SourcesPage";
import { TagsPage } from "../features/tags/TagsPage";
import { UsersPage } from "../features/users/UsersPage";

// Прототипы грузятся ОТДЕЛЬНЫМИ чанками (lazy): это статичные макеты, которые открывают по прямой
// ссылке считаные разы, и рабочее приложение не должно тащить их вес на каждой загрузке.
const AnalyticsPreview = lazy(() =>
  import("../features/previews/AnalyticsPreview").then((m) => ({ default: m.AnalyticsPreview })),
);
const TechnologiesPreview = lazy(() =>
  import("../features/previews/TechnologiesPreview").then((m) => ({ default: m.TechnologiesPreview })),
);

type ScreenId =
  | "articles" | "signal-radar" | "digest" | "documents" | "sources" | "source-candidates" | "source-agent" | "scoring" | "tags" | "users" | "jobs" | "maintenance"
  | "statistics" | "analytics-preview" | "tech-preview";

// Экраны только для администратора (настройка источников/скоринга/тегов, пользователи, операции).
// Прототипы (*-preview) тоже admin-only: это статичные макеты с ВЫМЫШЛЕННЫМИ данными, их не должен
// случайно открыть обычный пользователь и принять за настоящую аналитику.
const ADMIN_SCREENS = new Set<ScreenId>([
  "sources", "source-candidates", "source-agent", "scoring", "tags", "users", "jobs", "maintenance",
  // Приём файлов — admin-only и на сервере (POST /api/documents требует require_admin):
  // фронтовый гейт без серверного был бы дырой, а серверный без фронтового — кнопкой,
  // которая у обычного пользователя всегда отвечает 403.
  "documents",
  // Статистика сводная (видно работу КАЖДОГО пользователя) — решение владельца:
  // раздел только для администраторов. Серверный гейт на /api/stats/monthly тоже
  // require_admin: фронтовый гейт без серверного — это дыра (аудит изоляции 24.07).
  "statistics",
  "analytics-preview", "tech-preview",
]);

type ScreenDef = {
  id: ScreenId;
  label: string;
  eyebrow: string;
  title: string;
  description: string;
  status: string;
};

type NavGroup = {
  label: string;
  screens: ScreenId[];
};

const screens: ScreenDef[] = [
  {
    id: "articles",
    label: "Сигналы",
    eyebrow: "Editorial Flow",
    title: "Поток сигналов",
    description: "Рабочий каталог сигналов: фильтры, группировка, AI-суть, score и редактор статусов уже живут в новом интерфейсе.",
    status: "Экран активен",
  },
  {
    id: "digest",
    label: "Месячный дайджест",
    eyebrow: "Editorial Output",
    title: "Сборка выпуска",
    description: "Выборка материалов, preview, draft и экспорт опираются на тот же backend API, но уже через новый интерфейс.",
    status: "Экран активен",
  },
  {
    id: "signal-radar",
    label: "Радар сигналов",
    eyebrow: "Signal Discovery",
    title: "Радар сигналов",
    description: "Сигналы, найденные новым поисковым агентом: evidence, переносимость, развернутая ОС и выбор в дайджест.",
    status: "Экран активен",
  },
  {
    id: "documents",
    label: "Материалы",
    eyebrow: "Documents",
    title: "Материалы",
    description: "Загрузка внутренних документов и их разбор: паспорт, суть, сводка и факты с привязкой к месту в файле.",
    status: "Экран активен",
  },
  {
    id: "sources",
    label: "Источники",
    eyebrow: "Source Control",
    title: "Каталог источников",
    description: "Упрощённые карточки, фильтры, диагностика и настройки парсинга теперь собраны в отдельном экране.",
    status: "Экран активен",
  },
  {
    id: "source-agent",
    label: "Агент источников",
    eyebrow: "Source Discovery",
    title: "Агент поиска источников",
    description: "Поиск новых источников, проверка кандидатов, память агента и рекомендации по действиям доступны только администраторам.",
    status: "Экран активен",
  },
  {
    id: "scoring",
    label: "Скоринг",
    eyebrow: "Config Surface",
    title: "Профиль критериев",
    description: "Весы критериев, редактор описаний и нормализация профиля уже перенесены в новый интерфейс.",
    status: "Экран активен",
  },
  {
    id: "tags",
    label: "Теги",
    eyebrow: "Taxonomy",
    title: "Дерево тем",
    description: "Parent/subtag структура, включение, редактирование и сохранение теперь тоже в новом интерфейсе.",
    status: "Экран активен",
  },
  {
    id: "users",
    label: "Пользователи",
    eyebrow: "Access",
    title: "Пользователи",
    description: "Управление учётными записями и ролями (только для администратора).",
    status: "Экран активен",
  },
  {
    id: "statistics",
    label: "Статистика",
    eyebrow: "Analytics",
    title: "Статистика платформы",
    description: "Месячные результаты платформы и работа пользователей: воронка от сбора до дайджеста, затраты на ИИ, разметка по каждому и общий итог (только для администратора).",
    status: "Экран активен",
  },
  {
    id: "tech-preview",
    label: "Технологии",
    eyebrow: "Prototype",
    title: "Технологии",
    description: "Статичный прототип каталога технологий: демонстрационные данные, без логики.",
    status: "Прототип",
  },
  {
    id: "analytics-preview",
    label: "Аналитика для БРБ",
    eyebrow: "Prototype",
    title: "Аналитика для БРБ",
    description: "Статичный прототип раздела аналитики: демонстрационные данные, без логики.",
    status: "Прототип",
  },
];

const appHighlights = [
  "Общий auth gate и session flow",
  "Typed API client поверх текущего backend",
  "Источники и диагностика",
  "Месячный дайджест и экспорт",
  "Скоринг и дерево тегов",
  "Каталог сигналов",
];

const navGroups: NavGroup[] = [
  {
    label: "Работа",
    screens: ["articles", "signal-radar", "digest", "documents"],
  },
  {
    label: "Настройки",
    screens: ["sources", "scoring", "tags"],
  },
  {
    label: "Администрирование",
    screens: ["users", "statistics", "source-agent"],
  },
  // Прототипы будущих разделов. Оба экрана в ADMIN_SCREENS, поэтому у не-админа фильтр ниже
  // (isAdmin || !ADMIN_SCREENS.has(sid)) вычистит их, visibleScreens станет пустым и вся группа
  // не отрисуется — обычный пользователь даже не увидит, что она существует.
  {
    label: "Прототипы",
    screens: ["tech-preview", "analytics-preview"],
  },
];

// Экраны, адресуемые через ?screen=<id>. jobs/maintenance в меню нет (служебные, только по ссылке);
// прототипы в меню есть, но параметр им нужен, чтобы ссылкой можно было поделиться для показа.
const URL_ADDRESSABLE: ScreenId[] = ["jobs", "maintenance", "tech-preview", "analytics-preview", "sources", "documents", "source-candidates", "source-agent", "signal-radar"];

function initialScreenFromUrl(): ScreenId {
  const value = new URLSearchParams(window.location.search).get("screen");
  return URL_ADDRESSABLE.find((id) => id === value) ?? "articles";
}

export function App() {
  const isTasksApp = window.location.pathname.replace(/\/+$/, "") === "/tasks";
  const [activeScreen, setActiveScreen] = useState<ScreenId>(initialScreenFromUrl);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [toast, setToast] = useState<{ text: string; tone: "default" | "error" } | null>(null);
  const [articles, setArticles] = useState<Article[]>([]);
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const active = screens.find((screen) => screen.id === activeScreen) ?? screens[0];
  const isAdmin = (user?.role ?? "user") === "admin";

  useEffect(() => {
    void loadSession();
  }, []);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 2800);
    return () => window.clearTimeout(timer);
  }, [toast]);

  let currentScreen = (
    <section className="screenStack">
      <header className="screenHeader">
        <div>
          <div className="eyebrow">{active.eyebrow}</div>
          <h1>{active.title}</h1>
          <p>{active.description}</p>
        </div>
        <div className="statusPill">{active.status}</div>
      </header>

      <section className="heroGrid">
        <article className="panel accentPanel">
          <div className="panelKicker">Админ-панель</div>
          <h2>Новая админ-панель уже собрана вокруг текущего API</h2>
          <p>
            Новый интерфейс уже покрывает ключевые редакторские и конфигурационные сценарии.
            Старая страница пока остаётся как запасной вариант, но основной каркас интерфейса уже здесь.
          </p>
        </article>

        <article className="panel">
          <div className="panelKicker">Что уже есть</div>
          <p>
            Мы переехали без смены серверных контрактов: это упрощает проверку соответствия и даёт
            возможность спокойно дотягивать поведение экран за экраном.
          </p>
        </article>
      </section>

      <section className="panel">
        <div className="panelHeader">
          <h2>Текущее покрытие</h2>
          <span className="badge">{appHighlights.length} зон</span>
        </div>
        <div className="stepList">
          {appHighlights.map((step, index) => (
            <div className="stepCard" key={step}>
              <div className="stepIndex">{String(index + 1).padStart(2, "0")}</div>
              <div className="stepText">{step}</div>
            </div>
          ))}
        </div>
      </section>
    </section>
  );

  if (activeScreen === "sources") {
    currentScreen = <SourcesPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "source-candidates") {
    currentScreen = <SourceCandidatesPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "source-agent") {
    currentScreen = <SourceAgentPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "documents") {
    currentScreen = <DocumentsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "articles") {
    currentScreen = (
      <ArticlesPage
        onUnauthorized={resetSession}
        showToast={showToast}
        initialArticles={articles}
        initialStats={stats}
        onArticlesReloaded={setArticles}
        onStatsReloaded={setStats}
      />
    );
  }

  if (activeScreen === "signal-radar") {
    currentScreen = <SignalRadarPage onUnauthorized={resetSession} showToast={showToast} isAdmin={isAdmin} />;
  }

  if (activeScreen === "digest") {
    currentScreen = <DigestPage onUnauthorized={resetSession} showToast={showToast} onArticlesChanged={() => void loadDashboardData()} isAdmin={isAdmin} />;
  }

  if (activeScreen === "scoring") {
    currentScreen = <ScoringPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "tags") {
    currentScreen = <TagsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "jobs") {
    currentScreen = <JobsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  if (activeScreen === "maintenance") {
    currentScreen = <MaintenancePage onUnauthorized={resetSession} showToast={showToast} />;
  }

  // Статичные прототипы будущих разделов (демо-данные, без логики и без обращений к API).
  if (activeScreen === "analytics-preview") {
    currentScreen = (
      <Suspense fallback={<div className="splashScreen">Загружаем прототип…</div>}>
        <AnalyticsPreview />
      </Suspense>
    );
  }

  if (activeScreen === "tech-preview") {
    currentScreen = (
      <Suspense fallback={<div className="splashScreen">Загружаем прототип…</div>}>
        <TechnologiesPreview />
      </Suspense>
    );
  }

  if (activeScreen === "users") {
    currentScreen = <UsersPage onUnauthorized={resetSession} showToast={showToast} currentUserId={Number(user?.id ?? 0)} />;
  }

  if (activeScreen === "statistics") {
    currentScreen = <StatisticsPage onUnauthorized={resetSession} showToast={showToast} />;
  }

  // Защита: не-админ не должен видеть админ-экраны даже по прямой ссылке ?screen=.
  if (!isAdmin && ADMIN_SCREENS.has(activeScreen)) {
    currentScreen = (
      <section className="screenStack">
        <header className="screenHeader">
          <div><h1>Нет доступа</h1></div>
        </header>
        <section className="panel">
          <div className="emptyState">Раздел доступен только администратору. Обратитесь к администратору системы.</div>
        </section>
      </section>
    );
  }

  function switchScreen(screenId: ScreenId) {
    setActiveScreen(screenId);
    if (URL_ADDRESSABLE.includes(screenId)) {
      window.history.replaceState(null, "", `?screen=${screenId}`);
      return;
    }
    if (URL_ADDRESSABLE.some((id) => window.location.search.includes(`screen=${id}`))) {
      window.history.replaceState(null, "", window.location.pathname || "/");
    }
  }

  async function loadSession() {
    try {
      setAuthLoading(true);
      const payload = await getSession();
      setUser(payload.user);
      if (!isTasksApp) {
        await loadDashboardData();
      }
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        showToast(error instanceof Error ? error.message : "Не удалось загрузить сессию", "error");
      }
      setUser(null);
    } finally {
      setAuthLoading(false);
    }
  }

  async function submitAuth() {
    try {
      const payload = authMode === "register" ? await register(email.trim(), password) : await login(email.trim(), password);
      setUser(payload.user);
      if (!isTasksApp) {
        await loadDashboardData();
      }
      setPassword("");
      showToast(authMode === "register" ? "Регистрация завершена" : "Вход выполнен");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Не удалось выполнить вход", "error");
    }
  }

  // Сброс сессии при 401. ВАЖНО: чистим не только user, но и данные — иначе после
  // протухания куки в открытой вкладке в стейте остаются статьи и счётчики прежнего
  // пользователя, и следующий вошедший видит их кадром до прихода своих данных
  // (аудит изоляции 24.07: handleLogout чистил, а onUnauthorized — нет).
  function resetSession() {
    setUser(null);
    setArticles([]);
    setStats(null);
  }

  async function handleLogout() {
    try {
      await logout();
      setUser(null);
      setArticles([]);
      setStats(null);
      showToast("Сессия завершена");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Не удалось выйти", "error");
    }
  }

  async function loadDashboardData() {
    const [articlesPayload, statsPayload] = await Promise.all([listArticles(DEFAULT_SIGNAL_ARTICLE_QUERY), getDashboardStats()]);
    setArticles(articlesPayload);
    setStats(statsPayload);
  }

  function showToast(text: string, tone: "default" | "error" = "default") {
    setToast({ text, tone });
  }

  if (authLoading) {
    return <div className="splashScreen">Проверяем сессию…</div>;
  }

  if (!user) {
    return (
      <div className="authShellReact">
        <div className="authCardReact">
          <div className="eyebrow">OilTech Digest</div>
          <h1>{authMode === "register" ? "Регистрация" : isTasksApp ? "Вход в трекер задач" : "Вход в админ-панель"}</h1>
          <p>
            {authMode === "register"
              ? "Создайте аккаунт, чтобы работать с задачами и редакторскими инструментами."
              : isTasksApp
                ? "Войдите в аккаунт, чтобы открыть скрытую доску проекта."
                : "Войдите в аккаунт, чтобы продолжить работу с редакторской панелью."}
          </p>
          <label className="field">
            <span>Эл. почта</span>
            <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="you@example.com" />
          </label>
          <label className="field">
            <span>Пароль</span>
            <input
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              placeholder="Не короче 8 символов"
            />
          </label>
          <div className="authActions">
            <button type="button" className="primaryButton" onClick={() => void submitAuth()}>
              {authMode === "register" ? "Зарегистрироваться" : "Войти"}
            </button>
          </div>
          {/* Переключателя «Зарегистрироваться» больше нет: самостоятельная
              регистрация закрыта (#33), и кнопка вела в 403. Учётки заводит
              администратор — экран «Пользователи» или CLI create-user. */}
          <div className="authSwitchText">
            Нет доступа? Обратитесь к администратору платформы.
          </div>
        </div>
        {toast ? <div className={`toastReact ${toast.tone === "error" ? "error" : ""}`}>{toast.text}</div> : null}
      </div>
    );
  }

  if (isTasksApp) {
    return (
      <div className="tasksAppShell">
        <header className="tasksAppTopbar">
          <div className="brand">
            <div className="brandMark">OT</div>
            <div className="brandText">
              <div className="brandTitle">OilTech Digest</div>
              <div className="brandSubtitle">Трекер задач</div>
            </div>
          </div>
          <div className="panelActions">
            <div className="statusPill">{user.email}</div>
            <button type="button" className="ghostButton compactButton" onClick={() => void handleLogout()}>
              Выйти
            </button>
          </div>
        </header>
        <main className="tasksAppContent">
          <BacklogPage onUnauthorized={resetSession} showToast={showToast} />
        </main>
        {toast ? <div className={`toastReact ${toast.tone === "error" ? "error" : ""}`}>{toast.text}</div> : null}
      </div>
    );
  }

  return (
    <div className={sidebarCollapsed ? "shell sidebarCollapsed" : "shell"}>
      <aside className="sidebar">
        <div className="sidebarTopRow">
          <div className="brand">
            <div className="brandMark">OT</div>
            <div className="brandText">
              <div className="brandTitle">OilTech Digest</div>
              <div className="brandSubtitle">Админ-панель</div>
            </div>
          </div>
        </div>

        <div className="brandMobileDivider" />

        <div className="sidebarGroups">
          {navGroups.map((group) => {
            const visibleScreens = group.screens.filter((sid) => isAdmin || !ADMIN_SCREENS.has(sid));
            if (!visibleScreens.length) return null;
            return (
            <section className="sidebarGroup" key={group.label}>
              <div className="sidebarSection">{group.label}</div>
              <nav className="nav">
                {visibleScreens.map((screenId) => {
                  const screen = screens.find((item) => item.id === screenId);
                  if (!screen) return null;
                  return (
                    <button
                      key={screen.id}
                      type="button"
                      className={screen.id === activeScreen ? "navButton active" : "navButton"}
                      onClick={() => switchScreen(screen.id)}
                      title={sidebarCollapsed ? screen.label : undefined}
                    >
                      <span className="navButtonIcon">
                        <ScreenIcon screenId={screen.id} />
                      </span>
                      <span className="navButtonLabel">{screen.label}</span>
                    </button>
                  );
                })}
              </nav>
            </section>
            );
          })}
        </div>

        <div className="sidebarBottom">
          <div className="sidebarFoot">
            <div className="footLabel">{user.email}</div>
            <div className="footValue">Сессия активна</div>
          </div>

          <div className="sidebarUtilityActions">
            <button
              type="button"
              className="navButton utilityButton"
              onClick={() => setSidebarCollapsed((value) => !value)}
              aria-label={sidebarCollapsed ? "Развернуть сайдбар" : "Свернуть сайдбар"}
              title={sidebarCollapsed ? "Развернуть сайдбар" : undefined}
            >
              <span className="navButtonIcon">
                <UtilityIcon kind="toggle" collapsed={sidebarCollapsed} />
              </span>
              <span className="navButtonLabel">{sidebarCollapsed ? "Развернуть меню" : "Свернуть меню"}</span>
            </button>
            <button
              type="button"
              className="navButton utilityButton danger"
              onClick={() => void handleLogout()}
              title={sidebarCollapsed ? "Выйти" : undefined}
            >
              <span className="navButtonIcon">
                <UtilityIcon kind="logout" collapsed={sidebarCollapsed} />
              </span>
              <span className="navButtonLabel">Выйти</span>
            </button>
          </div>
        </div>
      </aside>

      <main className="content">{currentScreen}</main>
      <nav className="mobileNav">
        {screens.filter((screen) => isAdmin || !ADMIN_SCREENS.has(screen.id)).map((screen) => (
          <button
            key={screen.id}
            type="button"
            className={screen.id === activeScreen ? "mobileNavButton active" : "mobileNavButton"}
            onClick={() => switchScreen(screen.id)}
            aria-label={screen.label}
          >
            <span className="mobileNavIcon">
              <ScreenIcon screenId={screen.id} />
            </span>
            <span className="mobileNavLabel">{screen.label}</span>
          </button>
        ))}
      </nav>
      {toast ? <div className={`toastReact ${toast.tone === "error" ? "error" : ""}`}>{toast.text}</div> : null}
    </div>
  );
}

function UtilityIcon(props: { kind: "toggle" | "logout"; collapsed: boolean }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.7 };

  if (props.kind === "toggle") {
    return (
      <svg
        {...common}
        style={{ transform: props.collapsed ? "rotate(180deg)" : "none", transition: "transform 220ms ease" }}
      >
        <path d="M10.5 3.5 5.5 8l5 4.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <path d="M6 3.5H4.8A1.8 1.8 0 0 0 3 5.3v5.4a1.8 1.8 0 0 0 1.8 1.8H6" strokeLinecap="round" />
      <path d="M8.2 5.2 11 8l-2.8 2.8M11 8H6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ScreenIcon(props: { screenId: ScreenId }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.6 };

  if (props.screenId === "articles") {
    return (
      <svg {...common}>
        <rect x="2.5" y="2.5" width="11" height="11" rx="2.2" />
        <path d="M5 6h6M5 8.5h6M5 11h4" />
      </svg>
    );
  }

  if (props.screenId === "digest") {
    return (
      <svg {...common}>
        <path d="M8 2.5 9.2 5l2.8.3-2 2 .5 2.7L8 8.7 5.5 10l.5-2.7-2-2L6.8 5 8 2.5Z" />
      </svg>
    );
  }

  if (props.screenId === "sources") {
    return (
      <svg {...common}>
        <path d="M3 4.5h10M3 8h10M3 11.5h6" />
        <circle cx="11.5" cy="11.5" r="2" />
      </svg>
    );
  }

  if (props.screenId === "documents") {
    return (
      <svg {...common}>
        <path d="M4 2.5h5l3 3v8H4z" strokeLinejoin="round" />
        <path d="M9 2.5v3h3M6 8h4M6 10.5h4" />
      </svg>
    );
  }

  if (props.screenId === "scoring") {
    return (
      <svg {...common}>
        <path d="M3 12.5 6.2 8.5l2.2 2.2L13 5.5" />
        <path d="M10.5 5.5H13v2.5" />
      </svg>
    );
  }

  if (props.screenId === "users") {
    return (
      <svg {...common}>
        <circle cx="6" cy="6" r="2.3" />
        <path d="M2.5 13c0-2.2 1.6-3.4 3.5-3.4s3.5 1.2 3.5 3.4" />
        <path d="M10.5 5.4c1.3 0 2.2 1 2.2 2.2s-.9 2.1-2.2 2.1" />
        <path d="M11.4 9.9c1.4.2 2.3 1.3 2.3 3.1" />
      </svg>
    );
  }

  return (
    <svg {...common}>
      <path d="M3 4.5h10M3 8h7M3 11.5h5" />
      <path d="M11 9.5h2.5V12H11z" />
    </svg>
  );
}
