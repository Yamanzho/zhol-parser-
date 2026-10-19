import { useEffect, useState, type ReactNode } from "react";
import { clearToken, endLocalSession, ensureLocalSession, getToken, isLocalHost, setToken, setUnauthorized, api } from "./api";
import {
  AuditDetail,
  Audits,
  Companies,
  CompanyDetail,
  Content,
  Dashboard,
  Listings,
  OrderDetail,
  Orders,
  ReportDetail,
  Reports,
  Scout,
  ScoutDetail,
  Tasks,
} from "./pages";
import { parseRoute, useHash } from "./ui";

const NAV = [
  ["scout", "Сбор компаний"],
  ["companies", "Компании"],
  ["history", "История"],
  ["dashboard", "Главная"],
  ["audits", "Аудит"],
];

type Gate = "checking" | "ready" | "login";

export function App(): ReactNode {
  const [gate, setGate] = useState<Gate>(getToken() ? "ready" : "checking");
  const hash = useHash();
  const route = parseRoute(hash);
  setUnauthorized(() => setGate("login"));

  useEffect(() => {
    if (getToken()) {
      setGate("ready");
      return;
    }
    let cancel = false;
    void ensureLocalSession().then((ok) => {
      if (!cancel) setGate(ok ? "ready" : "login");
    });
    return () => {
      cancel = true;
    };
  }, []);

  if (gate === "checking") {
    return (
      <form className="login">
        <h1>Жол</h1>
        <p>Открываем локальную панель.</p>
      </form>
    );
  }

  if (gate === "login") {
    return <Login onSuccess={() => setGate("ready")} />;
  }

  return (
    <div className="layout">
      <header className="topbar">
        <strong>Жол</strong>
        {NAV.map(([id, label]) => (
          <a
            key={id}
            href={`#/${id}`}
            className={route.page === id ? "active" : ""}
            title={id === "audits" ? "Загрузка и анализ аудита" : undefined}
          >
            {label}
          </a>
        ))}
        <button
          type="button"
          onClick={() => {
            clearToken();
            void endLocalSession().finally(() => setGate("login"));
          }}
        >
          Выйти
        </button>
      </header>
      <main>{screen(route.page, route.id)}</main>
    </div>
  );
}

function screen(page: string, id: string): ReactNode {
  if (page === "companies" && id) return <CompanyDetail id={id} />;
  if (page === "companies") return <Companies />;
  if (page === "history") return <Scout />;
  if (page === "scout" && id) return <ScoutDetail id={id} />;
  if (page === "scout") return <Scout />;
  if (page === "audits" && id) return <AuditDetail id={id} />;
  if (page === "audits") return <Audits />;
  if (page === "orders" && id) return <OrderDetail id={id} />;
  if (page === "orders") return <Orders />;
  if (page === "content") return <Content />;
  if (page === "listings") return <Listings />;
  if (page === "tasks") return <Tasks />;
  if (page === "reports" && id) return <ReportDetail id={id} />;
  if (page === "reports") return <Reports />;
  return <Dashboard />;
}

function Login(props: { onSuccess: () => void }): ReactNode {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const local = isLocalHost();

  if (local) {
    return (
      <form
        className="login"
        onSubmit={(event) => {
          event.preventDefault();
          setError("");
          void ensureLocalSession().then((ok) => {
            if (ok) props.onSuccess();
            else setError("Локальный вход недоступен. Закройте панель и запустите Жол еще раз.");
          });
        }}
      >
        <h1>Жол</h1>
        <p>Локальная панель на этом компьютере. Токен вводить не нужно.</p>
        {error && <p className="error">{error}</p>}
        <button type="submit">Открыть панель</button>
      </form>
    );
  }

  return (
    <form
      className="login"
      onSubmit={(event) => {
        event.preventDefault();
        if (!value.trim()) {
          setError("Введите токен администратора.");
          return;
        }
        setToken(value);
        void api("/api/v1/admin/summary")
          .then(() => {
            setError("");
            props.onSuccess();
          })
          .catch((reason: Error) => {
            clearToken();
            setError(reason.message);
          });
      }}
    >
      <h1>Жол</h1>
      <p>Этот адрес не локальный. Для входа нужен токен администратора. Токен не сохраняется в странице.</p>
      <label>
        Токен администратора
        <input type="password" value={value} onChange={(event) => setValue(event.target.value)} autoComplete="current-password" />
      </label>
      {error && <p className="error">{error}</p>}
      <button type="submit">Войти</button>
    </form>
  );
}
