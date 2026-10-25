import { useEffect, useState, type ReactNode } from "react";
import { clearToken, endLocalSession, ensureLocalSession, getToken, isLocalHost, setToken, setUnauthorized, api } from "./api";
import { Advanced, Collection, Companies as CompanyList, ExportPanel, MetaProvider, type Meta } from "./home";
import {
  AuditDetail,
  Audits,
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
  ["collect", "Сбор"],
  ["companies", "Компании"],
  ["export", "Выгрузка"],
  ["advanced", "Дополнительно"],
];

type Gate = "checking" | "ready" | "login";

export function App(): ReactNode {
  const [gate, setGate] = useState<Gate>(getToken() ? "ready" : "checking");
  const [meta, setMeta] = useState<Meta | null>(null);
  const hash = useHash();
  const route = parseRoute(hash);
  setUnauthorized(() => setGate("login"));

  useEffect(() => {
    let cancel = false;
    void fetch("/api/v1/meta")
      .then(async (response) => (response.ok ? ((await response.json()) as Meta) : null))
      .then((body) => {
        if (cancel) return;
        setMeta(body);
        if (body?.demo) {
          setGate("ready");
          return;
        }
        if (getToken()) {
          setGate("ready");
          return;
        }
        void ensureLocalSession().then((ok) => {
          if (!cancel) setGate(ok ? "ready" : "login");
        });
      })
      .catch(() => {
        if (cancel) return;
        if (getToken()) setGate("ready");
        else {
          void ensureLocalSession().then((ok) => {
            if (!cancel) setGate(ok ? "ready" : "login");
          });
        }
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
    <MetaProvider value={meta}>
      <div className="layout">
        <header className="topbar">
          <strong>Жол</strong>
          {NAV.map(([id, label]) => (
            <a key={id} href={`#/${id}`} className={route.page === id ? "active" : ""}>
              {label}
            </a>
          ))}
          {!meta?.demo && (
            <button
              type="button"
              onClick={() => {
                clearToken();
                void endLocalSession().finally(() => setGate("login"));
              }}
            >
              Выйти
            </button>
          )}
        </header>
        {meta?.demo && <div className="demo-banner">Демонстрационный режим. Учебные компании, без карт и без рабочей базы.</div>}
        <main>{screen(route.page, route.id)}</main>
      </div>
    </MetaProvider>
  );
}

function screen(page: string, id: string): ReactNode {
  if (page === "collect") return <Collection />;
  if (page === "companies" && id) return <CompanyDetail id={id} />;
  if (page === "companies") return <CompanyList />;
  if (page === "export") return <ExportPanel />;
  if (page === "advanced") return <Advanced />;
  if (page === "history" || (page === "scout" && !id)) return <Scout />;
  if (page === "scout" && id) return <ScoutDetail id={id} />;
  if (page === "audits" && id) return <AuditDetail id={id} />;
  if (page === "audits") return <Audits />;
  if (page === "orders" && id) return <OrderDetail id={id} />;
  if (page === "orders") return <Orders />;
  if (page === "content") return <Content />;
  if (page === "listings") return <Listings />;
  if (page === "tasks") return <Tasks />;
  if (page === "reports" && id) return <ReportDetail id={id} />;
  if (page === "reports") return <Reports />;
  if (page === "dashboard") return <Dashboard />;
  return <Collection />;
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
