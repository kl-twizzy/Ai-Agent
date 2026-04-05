import { useEffect, useRef, useState } from "react";
import axios from "axios";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
  headers: {
    "Content-Type": "application/json",
  },
});

const suggestedPrompts = [
  "Открой сайт cbr.ru и найди официальный курс доллара на сегодня",
  "Зайди на YouTube и найди видео про квантовую физику",
  "Открой маркетплейс и найди рабочий ноутбук до 70000 рублей",
  "Зайди на сайт РБК и открой свежие новости про искусственный интеллект",
  "Открой Кинопоиск и найди популярные фильмы этого года",
];

const STATUS_LABELS = {
  idle: "Ожидает задачу",
  pending: "Подготавливает запуск",
  running: "Агент работает",
  completed: "Выполнение завершено",
  failed: "Выполнение завершилось с ошибкой",
  error: "Ошибка запроса",
};

const extractExecutionId = (payload) =>
  payload?.execution_id || payload?.id || payload?.data?.execution_id || "";

const normalizeList = (value) => {
  if (!value) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
};

const stringifyValue = (value) => {
  if (value == null) {
    return "";
  }
  if (typeof value === "string") {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

const normalizeExecutionData = (payload) => {
  const result =
    payload?.result ||
    payload?.data?.result ||
    payload?.data?.output ||
    payload?.output ||
    payload?.data ||
    null;

  return {
    status: payload?.status || payload?.data?.status || "pending",
    error: payload?.error || payload?.message || payload?.detail || "",
    result,
    summary:
      result?.summary ||
      result?.answer ||
      result?.message ||
      payload?.summary ||
      payload?.data?.summary ||
      "",
    finalUrl:
      result?.final_url ||
      result?.url ||
      payload?.final_url ||
      payload?.data?.final_url ||
      "",
    currentUrl: result?.current_url || result?.page_url || payload?.current_url || "",
    steps: normalizeList(result?.steps || result?.history || payload?.steps),
    logs: normalizeList(result?.logs || payload?.logs),
    screenshot:
      result?.screenshot_url ||
      result?.screenshot ||
      payload?.screenshot_url ||
      payload?.data?.screenshot_url ||
      "",
  };
};

const formatStepLabel = (step, index) => {
  const label = step?.description || step?.action || `Шаг ${index + 1}`;
  return label;
};

export default function App() {
  const [query, setQuery] = useState("");
  const [executionId, setExecutionId] = useState("");
  const [status, setStatus] = useState("idle");
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");
  const [toastVisible, setToastVisible] = useState(false);
  const pollerRef = useRef(null);

  const isLoading = status === "pending" || status === "running";
  const statusLabel = STATUS_LABELS[status] || "Неизвестный статус";
  const structuredResult = result?.summary || result?.finalUrl || result?.currentUrl;
  const rawResult = stringifyValue(result?.result);
  const activityCount = result?.steps?.length || result?.logs?.length || 0;
  const latestStep = result?.steps?.[result.steps.length - 1];
  const surfaceStatus = isLoading
    ? "Идет живое выполнение"
    : executionId
      ? "Можно изучать результат"
      : "Ожидает запуск";

  useEffect(() => {
    return () => {
      if (pollerRef.current) {
        clearInterval(pollerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!toastVisible) {
      return undefined;
    }

    const timeoutId = window.setTimeout(() => {
      setToastVisible(false);
    }, 4000);

    return () => window.clearTimeout(timeoutId);
  }, [toastVisible]);

  const stopPolling = () => {
    if (pollerRef.current) {
      clearInterval(pollerRef.current);
      pollerRef.current = null;
    }
  };

  const showError = (message) => {
    setError(message);
    setToastVisible(true);
  };

  const fetchExecution = async (id) => {
    try {
      const response = await api.get(`/executions/${id}`);
      const payload = normalizeExecutionData(response.data);

      setStatus(payload.status);
      setResult(payload);

      if (payload.status === "completed") {
        stopPolling();
        return false;
      }

      if (payload.status === "failed" || payload.error) {
        stopPolling();
        setStatus("error");
        showError(payload.error || "Браузерный агент не смог выполнить задачу.");
        return false;
      }

      return true;
    } catch (requestError) {
      stopPolling();
      setStatus("error");
      showError(
        requestError.response?.data?.message ||
          requestError.message ||
          "Не удалось загрузить статус выполнения."
      );
      return false;
    }
  };

  const handleSearch = async (event) => {
    event.preventDefault();

    const trimmed = query.trim();
    if (!trimmed) {
      showError("Введите задачу для браузерного агента.");
      return;
    }

    stopPolling();
    setError("");
    setToastVisible(false);
    setResult(null);
    setExecutionId("");
    setStatus("pending");

    try {
      const response = await api.post("/execute", {
        task: trimmed,
        user_prompt: trimmed,
      });

      const id = extractExecutionId(response.data);
      if (!id) {
        throw new Error("Backend не вернул execution_id.");
      }

      setExecutionId(id);
      const shouldContinuePolling = await fetchExecution(id);

      if (shouldContinuePolling && !pollerRef.current) {
        pollerRef.current = window.setInterval(() => {
          fetchExecution(id);
        }, 2000);
      }
    } catch (requestError) {
      stopPolling();
      setStatus("error");
      showError(
        requestError.response?.data?.message ||
          requestError.message ||
          "Не удалось запустить браузерного агента."
      );
    }
  };

  const handlePromptSelect = (prompt) => {
    setQuery(prompt);
    setError("");
    setToastVisible(false);
  };

  return (
    <main className="app-shell">
      <div className="background-orb background-orb-top" />
      <div className="background-orb background-orb-bottom" />

      <div className="app-container">
        <section className="panel">
          <div className="hero-layout">
            <div className="hero">
              <p className="eyebrow">панель управления browser-agent</p>
              <h1>Управляйте браузером обычным текстом</h1>
              <p className="hero-copy">
                Попросите агента открыть сайт, найти информацию, перейти по ссылкам,
                взаимодействовать с интерфейсом и показать результат шаг за шагом.
              </p>
              <div className="hero-actions">
                <div className="hero-chip">
                  <span className="hero-chip-label">Режим работы</span>
                  <strong>LLM → MCP → браузер</strong>
                </div>
                <div className="hero-chip">
                  <span className="hero-chip-label">Backend</span>
                  <strong>{API_BASE_URL || "тот же домен"}</strong>
                </div>
              </div>
            </div>

            <aside className="showcase-card">
              <div className="showcase-topline">
                <span className="showcase-kicker">Состояние агента</span>
                <span className="showcase-state">{surfaceStatus}</span>
              </div>

              <div className="showcase-metrics">
                <div className="metric-tile">
                  <span className="metric-label">Статус</span>
                  <strong>{statusLabel}</strong>
                </div>
                <div className="metric-tile">
                  <span className="metric-label">Execution ID</span>
                  <strong>{executionId || "Еще не запущен"}</strong>
                </div>
                <div className="metric-tile">
                  <span className="metric-label">Активность</span>
                  <strong>{activityCount}</strong>
                </div>
              </div>

              <div className="device-stage">
                <div className="device device-desktop">
                  <span className="device-label">Рабочая сессия</span>
                  <div className="device-screen">
                    <div className="device-bar" />
                    <div className="device-wave device-wave-wide" />
                    <div className="device-wave" />
                    <div className="device-wave device-wave-thin" />
                  </div>
                </div>
                <div className="device-stack">
                  <div className="device device-tablet">
                    <span className="device-label">Прогресс</span>
                    <div className="device-screen">
                      <div className="device-wave device-wave-wide" />
                      <div className="device-wave" />
                    </div>
                  </div>
                  <div className="device device-phone">
                    <span className="device-label">Шаги</span>
                    <div className="device-screen">
                      <div className="device-bar" />
                      <div className="device-wave" />
                    </div>
                  </div>
                </div>
              </div>
            </aside>
          </div>

          <div className="signal-strip">
            <div className="signal-card">
              <span className="signal-label">Для пользователя</span>
              <strong>Одна строка ввода вместо ручной работы в браузере</strong>
            </div>
            <div className="signal-card">
              <span className="signal-label">Для демонстрации</span>
              <strong>Видны шаги, статус, итоговая страница и ход выполнения</strong>
            </div>
            <div className="signal-card">
              <span className="signal-label">Для разработки</span>
              <strong>MCP-инструменты и прозрачный цикл агентного исполнения</strong>
            </div>
          </div>

          <div className="top-grid">
            <section className="card">
              <div className="section-head">
                <div>
                  <p className="section-label">Примеры задач</p>
                  <h2>Попробуйте один из сценариев</h2>
                </div>
                <div className="pill">Нажмите, чтобы подставить</div>
              </div>

              <div className="prompt-list">
                {suggestedPrompts.map((prompt) => (
                  <button
                    key={prompt}
                    type="button"
                    className="prompt-chip"
                    onClick={() => handlePromptSelect(prompt)}
                  >
                    {prompt}
                  </button>
                ))}
              </div>
            </section>

            <aside className="card">
              <p className="section-label">Как это работает</p>
              <h2>Агент не просто ищет, а реально выполняет действия</h2>
              <p className="agent-copy">
                Backend принимает задачу, запускает агентный цикл, обращается к MCP-инструментам,
                управляет браузером и возвращает понятный результат вместе с шагами выполнения.
              </p>
              <div className="agent-steps">
                <div className="info-block">
                  Отправьте задачу на <code>/execute</code> и получите <code>execution_id</code>.
                </div>
                <div className="info-block">
                  Интерфейс опрашивает <code>/executions/{"{id}"}</code> и обновляет прогресс в реальном времени.
                </div>
                <div className="info-block">
                  Вы видите итог, текущую страницу, шаги агента и диагностические данные запуска.
                </div>
              </div>
            </aside>
          </div>

          <form className="search-form" onSubmit={handleSearch}>
            <label className="field">
              <span className="section-label">Задача</span>
              <textarea
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Например: открой маркетплейс и найди рабочий ноутбук до 70000 рублей"
                rows="4"
              />
            </label>

            <div className="form-sidecar">
              <div className="form-note">
                Пишите задачу так, как сказали бы человеку. Агент сам попробует открыть нужный сайт,
                понять контекст страницы и выбрать следующий шаг.
              </div>
              <button type="submit" className="search-button" disabled={isLoading}>
                {isLoading ? "Выполняется..." : "Запустить"}
              </button>
            </div>
          </form>

          <div className="content-grid">
            <section className="card">
              <div className="section-head">
                <div>
                  <p className="section-label">Выполнение</p>
                  <h2>{statusLabel}</h2>
                </div>
                <div className="pill">
                  {executionId ? `ID: ${executionId}` : "Здесь появится execution ID"}
                </div>
              </div>

              {isLoading ? (
                <div className="status-box">
                  <div className="spinner" />
                  <p className="status-title">Агент работает</p>
                  <p className="status-copy">
                    Интерфейс обновляет состояние каждые 2 секунды, пока backend не вернет итоговый результат.
                  </p>
                  {latestStep ? (
                    <div className="live-step">
                      <span className="live-step-label">Последний шаг</span>
                      <strong>{formatStepLabel(latestStep, result?.steps?.length || 0)}</strong>
                    </div>
                  ) : null}
                </div>
              ) : structuredResult || rawResult ? (
                <div className="result-box">
                  <p className="section-label">Результат</p>
                  <h3>Итог выполнения</h3>

                  {result?.summary ? <p className="result-link">{result.summary}</p> : null}

                  {result?.finalUrl ? (
                    <>
                      <p className="section-label result-meta-label">Финальный URL</p>
                      <p className="result-link">{result.finalUrl}</p>
                      <a
                        className="result-button"
                        href={result.finalUrl}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Открыть финальную страницу
                      </a>
                    </>
                  ) : null}

                  {result?.currentUrl ? (
                    <>
                      <p className="section-label result-meta-label">Текущая страница</p>
                      <p className="result-link">{result.currentUrl}</p>
                    </>
                  ) : null}

                  {rawResult && !structuredResult ? <pre className="result-pre">{rawResult}</pre> : null}
                </div>
              ) : (
                <div className="empty-box">
                  Отправьте задачу, и здесь появится итог работы браузерного агента.
                </div>
              )}
            </section>

            <aside className="card">
              <p className="section-label">Детали запуска</p>
              <div className="info-list">
                <div className="info-block">
                  Статус: <strong>{statusLabel}</strong>
                </div>
                <div className="info-block">
                  Контракт backend: <code>/execute</code> и <code>/executions/{"{id}"}</code>
                </div>
                {latestStep ? (
                  <div className="info-block">
                    Последнее действие:
                    <div className="step-badge">
                      <span>{latestStep.tool_name || latestStep.action || "step"}</span>
                      <strong>{latestStep.llm_mode || "runtime"}</strong>
                    </div>
                    <p className="mini-copy">{formatStepLabel(latestStep, result?.steps?.length || 0)}</p>
                  </div>
                ) : null}
                {result?.steps?.length ? (
                  <div className="info-block">
                    Шаги:
                    <pre className="debug-pre">
                      {result.steps.slice(0, 6).map((item) => stringifyValue(item)).join("\n\n")}
                    </pre>
                  </div>
                ) : null}
                {result?.logs?.length ? (
                  <div className="info-block">
                    Логи:
                    <pre className="debug-pre">
                      {result.logs.slice(0, 6).map((item) => stringifyValue(item)).join("\n\n")}
                    </pre>
                  </div>
                ) : null}
                {result?.screenshot ? (
                  <div className="info-block">
                    Скриншот:
                    <p className="result-link">Получен</p>
                  </div>
                ) : null}
              </div>
            </aside>
          </div>
        </section>

        <div className={`toast ${toastVisible ? "toast-visible" : ""}`} role="alert">
          <div className="toast-dot" />
          <div>
            <p className="toast-title">Ошибка выполнения</p>
            <p className="toast-copy">{error}</p>
          </div>
        </div>
      </div>
    </main>
  );
}
