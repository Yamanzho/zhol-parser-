import { useState, type ReactNode } from "react";
import type { Row } from "./api";
import {
  Banner,
  Feedback,
  Pager,
  StateView,
  cell,
  downloadAction,
  downloadPlatformWorkbook,
  rowsOf,
  sendAction,
  uploadAction,
  useAction,
  useGet,
} from "./ui";

const PLATFORMS = ["2gis", "yandex", "google"];
const PLATFORM_TITLE: Record<string, string> = {
  "2gis": "2ГИС",
  yandex: "Яндекс Карты",
  google: "Google Maps",
};
const CARD_LINK: Record<string, string> = {
  "2gis": "Открыть в 2ГИС",
  yandex: "Открыть в Яндекс Картах",
  google: "Открыть в Google Maps",
};
const CONTACT_FALLBACK =
  "«Запрет не установлен» значит, что запрета на связь в базе нет. Это не согласие на рекламу и не разрешение на рассылку.";
const CONSENT_FALLBACK = "Согласие на рассылку в базе не записано. Значение «Запрет не установлен» в согласие не превращается.";

type FactLink = { text: string; href: string };
type Fact = { label: string; text: string; href?: string; state?: string; links?: FactLink[] };
type Contact = { label: string; text: string; state?: string; links?: FactLink[] };
type Card = { platform: string; title: string; notice: string; facts: Fact[]; contacts?: Contact[] };
type HistoryItem = {
  id: number;
  when: string;
  source_label: string;
  verification_label: string;
  changes: string[];
  facts: Fact[];
  contacts?: Contact[];
};
type HistoryGroup = { platform: string; title: string; items: HistoryItem[]; empty?: string | null };

function asCards(value: unknown): Card[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === "object") as Card[];
}

function asHistory(value: unknown): HistoryGroup[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === "object") as HistoryGroup[];
}

function asFacts(value: unknown): Fact[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === "object") as Fact[];
}

function safeHref(value: unknown): string {
  if (typeof value !== "string") return "";
  const text = value.trim();
  if (text.startsWith("https://") || text.startsWith("http://")) return text;
  if (/^tel:\+\d{11,15}$/.test(text)) return text;
  if (/^mailto:[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}$/i.test(text)) return text;
  return "";
}

function factState(fact: { text: string; state?: string }): string {
  if (fact.state === "empty" || fact.state === "unchecked" || fact.state === "unverified") return fact.state;
  if (fact.text === "Не проверялось") return "unchecked";
  if (fact.text === "Данные требуют проверки" || fact.text === "Отклонено" || fact.text.includes("не актуальная проверка")) return "unverified";
  if (fact.text === "Нет данных" || fact.text === "Не найдена при проверке" || fact.text === "Скриншота нет.") return "empty";
  return "";
}

function asContacts(value: unknown): Contact[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === "object") as Contact[];
}

function FactList(props: { facts: Fact[]; contacts?: Contact[]; platform?: string }): ReactNode {
  const contacts = props.contacts || [];
  const facts = contacts.length > 0 ? props.facts.filter((fact) => fact.label !== "Контакты") : props.facts;
  return (
    <dl className="facts">
      {facts.map((fact) => (
        <FactItem key={fact.label} fact={shownFact(fact, props.platform)} />
      ))}
      {contacts.map((contact, index) => (
        <FactItem key={`${contact.label}-${index}`} fact={contact} />
      ))}
    </dl>
  );
}

function shownFact(fact: Fact, platform?: string): Fact {
  const href = safeHref(fact.href);
  if (fact.label === "Ссылка на карточку" && href) {
    return { ...fact, text: CARD_LINK[platform || ""] || "Открыть карточку" };
  }
  if ((fact.label === "Сайт" || fact.label === "Ссылка") && href && fact.text.length > 48) {
    return { ...fact, text: fact.label === "Сайт" ? "Открыть сайт" : "Открыть ссылку" };
  }
  return fact;
}

function FactItem(props: { fact: Fact }): ReactNode {
  const state = factState(props.fact);
  return (
    <div className="fact">
      <dt>{props.fact.label}</dt>
      <dd className={state ? `is-${state}` : undefined}>
        <FactValue fact={props.fact} />
      </dd>
    </div>
  );
}

function FactValue(props: { fact: Fact }): ReactNode {
  const links = (props.fact.links || [])
    .map((link) => ({ text: link.text, href: safeHref(link.href) }))
    .filter((link) => link.text && link.href);
  if (links.length > 0) {
    return (
      <>
        {links.map((link, index) => (
          <span key={`${link.href}-${index}`}>
            {index > 0 ? ", " : null}
            <a href={link.href}>{link.text}</a>
          </span>
        ))}
      </>
    );
  }
  const href = safeHref(props.fact.href);
  if (!href) return props.fact.text;
  return <a href={href}>{props.fact.text}</a>;
}

function platformTitle(platform: string): string {
  return PLATFORM_TITLE[platform] || platform || "Площадка";
}

function headerFacts(header: Row): Fact[] {
  const pairs: Array<[string, string]> = [
    ["Название", "name"],
    ["Сектор", "sector"],
    ["Категория", "category"],
    ["Подкатегория", "subcategory"],
    ["Город", "city"],
    ["Адрес", "address"],
    ["Район", "district"],
    ["Телефон", "phone"],
    ["WhatsApp", "whatsapp"],
    ["Instagram", "instagram"],
    ["Сайт", "website"],
    ["Почта", "email"],
    ["Статус связи", "contact_label"],
    ["Согласие на рассылку", "consent_label"],
    ["Номер в 2ГИС", "id_2gis_label"],
    ["Источник", "source_platform_label"],
    ["Совпадение", "matching_status"],
    ["Балл аудита", "audit_score"],
    ["Дата наблюдения", "audit_observed_on"],
  ];
  return pairs.map(([label, key]) => {
    const fact: Fact = { label, text: cell(header[key]) };
    if (key === "address" && header.address_verified === false && fact.text !== "Нет данных") {
      fact.text = `${fact.text} (собрано, не проверено)`;
    }
    const href = header[`${key}_href`];
    if (typeof href === "string") fact.href = href;
    const links = header[`${key}_links`];
    if (Array.isArray(links)) fact.links = links.filter((item) => item && typeof item === "object") as FactLink[];
    return shownFact(fact);
  });
}

export function Companies(): ReactNode {
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [offset, setOffset] = useState(0);
  const data = useGet<Row>(`/api/v1/admin/companies?q=${encodeURIComponent(term)}&limit=50&offset=${offset}`);
  const rows = rowsOf(data.data);
  const unresolved = Array.isArray(data.data?.unresolved_matches) ? (data.data.unresolved_matches as Row[]) : [];
  const hint = typeof data.data?.contact_hint === "string" ? data.data.contact_hint : CONTACT_FALLBACK;
  return (
    <section>
      <h1>Компании</h1>
      <Banner />
      <p className="hint">Поиск по названию, городу, категории, номеру 2ГИС и номеру на Яндексе или Google. Новые компании появляются после сбора.</p>
      <PlatformBoard />
      <form
        className="toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setTerm(q);
        }}
      >
        <label>
          Поиск
          <input value={q} placeholder="Название, город или номер площадки" onChange={(event) => setQ(event.target.value)} />
        </label>
        <button type="submit">Найти</button>
      </form>
      <StateView
        loading={data.loading}
        error={data.error}
        empty={!data.loading && !data.error && rows.length === 0}
        emptyText="Компании не найдены."
      />
      {unresolved.length > 0 && (
        <article className="panel">
          <h2>Нужна проверка</h2>
          <p className="hint">Эти находки не объединены с компанией: совпадение неоднозначно.</p>
          <ul>
            {unresolved.map((item) => (
              <li key={cell(item.item_key)}>
                {cell(item.name)}. {platformTitle(cell(item.platform))}. {cell(item.address)}. {cell(item.reason_label)}
              </li>
            ))}
          </ul>
        </article>
      )}
      {rows.length > 0 && (
        <>
          <div className="table-wrap">
            <table className="companies">
              <thead>
                <tr>
                  <th>Название</th>
                  <th>Категория</th>
                  <th>Город</th>
                  <th>Адрес</th>
                  <th>Телефон</th>
                  <th>Площадки</th>
                  <th>Источник</th>
                  <th>Совпадение</th>
                  <th title={hint}>Статус связи</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const marks = Array.isArray(row.platforms_view) ? (row.platforms_view as Row[]) : [];
                  return (
                    <tr key={cell(row.id)}>
                      <td>
                        <a href={`#/companies/${cell(row.id)}`}>{cell(row.name)}</a>
                        {row.id_2gis ? null : <span className="mark">{cell(row.id_2gis_label)}</span>}
                      </td>
                      <td>{cell(row.category)}</td>
                      <td>{cell(row.city)}</td>
                      <td title={typeof row.address_note === "string" ? row.address_note : undefined}>
                        {cell(row.address)}
                        {row.address_verified === false && cell(row.address) !== "Нет данных" ? (
                          <span className="mark">собрано, не проверено</span>
                        ) : null}
                        {cell(row.address) === "Нет данных" && typeof row.address_note === "string" && row.address_note.includes("разных адрес") ? (
                          <span className="mark">адреса расходятся</span>
                        ) : null}
                      </td>
                      <td>{cell(row.phone)}</td>
                      <td>
                        {marks.map((mark) => (
                          <span className="mark" key={cell(mark.platform)} title={cell(mark.label)}>
                            {cell(mark.label)}: {cell(mark.text)}
                          </span>
                        ))}
                      </td>
                      <td>{cell(row.source_platform_label)}</td>
                      <td>
                        {cell(row.matching_status)}
                        {Array.isArray(row.missing_platforms) && (row.missing_platforms as Row[]).length > 0 ? (
                          <span className="mark">
                            Не проверялось: {(row.missing_platforms as Row[]).map((item) => cell(item.label)).join(", ")}
                          </span>
                        ) : null}
                      </td>
                      <td title={hint}>
                        {cell(row.contact_label)}
                        <span className="mark">Согласие: {cell(row.consent_label)}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} total={Number(data.data?.total || 0)} limit={50} onChange={setOffset} />
        </>
      )}
    </section>
  );
}

function PlatformBoard(): ReactNode {
  const [platform, setPlatform] = useState("2gis");
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [offset, setOffset] = useState(0);
  const action = useAction();
  const data = useGet<Row>(
    `/api/v1/admin/scout/platforms/${platform}?q=${encodeURIComponent(term)}&limit=50&offset=${offset}`,
  );
  const rows = rowsOf(data.data);
  const tabs = [
    ["2gis", "2ГИС"],
    ["google", "Google Maps"],
  ];
  return (
    <article className="panel">
      <h2>Карточки по источникам</h2>
      <p className="hint">
        В каждой вкладке только поля снимка этой площадки. Общая компания не копируется, и значения одной площадки не подставляются в другую.
      </p>
      <div className="tabs">
        {tabs.map(([id, label]) => (
          <button
            className={platform === id ? "tab active" : "tab"}
            key={id}
            type="button"
            onClick={() => {
              setPlatform(id);
              setOffset(0);
            }}
          >
            {label}
          </button>
        ))}
      </div>
      <p>
        {cell(data.data?.label)}: {cell(data.data?.total)} записей.
      </p>
      <form
        className="toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setTerm(q);
        }}
      >
        <label>
          Поиск в этой вкладке
          <input value={q} placeholder="Название, телефон, адрес" onChange={(event) => setQ(event.target.value)} />
        </label>
        <button type="submit">Найти</button>
      </form>
      <div className="actions">
        <button
          type="button"
          disabled={action.pending}
          onClick={() =>
            void action.run(async () => {
              const body = await sendAction("/api/v1/admin/scout/platform-export");
              const sheets = (body.sheets || {}) as Row;
              return `Лист 2ГИС: ${cell(sheets["2gis"])}. Лист Google Maps: ${cell(sheets.google)}.`;
            }, "Собрать Excel с отдельными листами 2ГИС и Google Maps?")
          }
        >
          Подготовить Excel
        </button>
        <button
          type="button"
          disabled={action.pending}
          onClick={() => void action.run(() => downloadPlatformWorkbook())}
        >
          Скачать Excel
        </button>
      </div>
      <p className="hint">Скачивается новый файл 2gis-google.xlsx. Служебная копия и старые загрузки не являются этой таблицей.</p>
      <Feedback message={action.message} error={action.error} />
      <StateView
        loading={data.loading}
        error={data.error}
        empty={!data.loading && !data.error && rows.length === 0}
        emptyText="В этой вкладке записей нет."
      />
      {rows.length > 0 && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Название</th>
                  <th>Категория</th>
                  <th>Поисковый запрос</th>
                  <th>Источник</th>
                  <th>Город</th>
                  <th>Регион</th>
                  <th>Адрес</th>
                  <th>Телефон</th>
                  <th>WhatsApp</th>
                  <th>Instagram</th>
                  <th>Ссылка на карточку</th>
                  <th>Рейтинг</th>
                  <th>Отзывы</th>
                  <th>Дата сбора</th>
                  <th>Полнота</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => {
                  const href = typeof row.source_url === "string" ? row.source_url : "";
                  return (
                    <tr key={cell(row.company_id)}>
                      <td>
                        <a href={`#/companies/${cell(row.company_id)}`}>{cell(row.name)}</a>
                      </td>
                      <td>{cell(row.category)}</td>
                      <td>{cell(row.search_query)}</td>
                      <td>{cell(row.source)}</td>
                      <td>{cell(row.city)}</td>
                      <td>{cell(row.region)}</td>
                      <td>{cell(row.address)}</td>
                      <td>{cell(row.phone)}</td>
                      <td>{cell(row.whatsapp)}</td>
                      <td>{cell(row.instagram)}</td>
                      <td>{href.startsWith("http://") || href.startsWith("https://") ? <a href={href}>{href}</a> : cell(row.source_url)}</td>
                      <td>{cell(row.rating)}</td>
                      <td>{cell(row.reviews_count)}</td>
                      <td>{cell(row.collected_at)}</td>
                      <td>{cell(row.completeness_label)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <Pager offset={offset} total={Number(data.data?.total || 0)} limit={50} onChange={setOffset} />
        </>
      )}
    </article>
  );
}

export function CompanyDetail(props: { id: string }): ReactNode {
  const [tick, setTick] = useState(0);
  const action = useAction(() => setTick((value) => value + 1));
  const data = useGet<Row>(`/api/v1/admin/companies/${props.id}?refresh=${tick}`);
  const [platform, setPlatform] = useState("2gis");
  const header = (data.data?.header || {}) as Row;
  const cards = asCards(data.data?.cards);
  const history = asHistory(data.data?.history);
  const audit = (data.data?.historical_audit || {}) as Row;
  const active = cards.find((card) => card.platform === platform) || cards[0];
  const hint = typeof data.data?.contact_hint === "string" ? data.data.contact_hint : CONTACT_FALLBACK;
  const consentHint = typeof data.data?.consent_hint === "string" ? data.data.consent_hint : CONSENT_FALLBACK;
  const workspace = (data.data?.workspace || {}) as Row;
  const texts = Array.isArray(audit.texts) ? (audit.texts as unknown[]) : [];
  return (
    <section>
      <h1>{data.data ? cell(header.name) : `Компания ${props.id}`}</h1>
      <Banner />
      <p>
        <a href="#/companies">К списку компаний</a>
      </p>
      <StateView loading={data.loading} error={data.error} />
      {data.data && (
        <>
          <p className="hint" title={hint}>
            {cell(header.scope_note)} Статус связи: {cell(header.contact_label)}. {hint}
          </p>
          <p className="hint">{consentHint}</p>
          <p className="muted">{cell(header.address_note)}</p>
          <FactList facts={headerFacts(header)} />
          <IdentityPanel header={header} />
          <p className="muted">{cell(header.audit_note)}</p>
          <h2>Карточки на площадках</h2>
          <div className="tabs" role="tablist">
            {PLATFORMS.map((name) => {
              const card = cards.find((item) => item.platform === name);
              return (
                <button
                  key={name}
                  type="button"
                  className={name === (active?.platform || platform) ? "tab active" : "tab"}
                  onClick={() => setPlatform(name)}
                >
                  {card?.title || name}
                </button>
              );
            })}
          </div>
          {active ? (
            <article className="panel">
              <p className="hint notice">{active.notice}</p>
              <FactList facts={asFacts(active.facts)} contacts={asContacts(active.contacts)} platform={active.platform} />
            </article>
          ) : (
            <p>Данных по площадкам нет.</p>
          )}
          <h2>Балл аудита</h2>
          <p className="hint">{cell(audit.note)}</p>
          {audit.shown === true ? (
            <article className="panel">
              <p>
                Дата: {cell(audit.observed_on)}. Источник: {cell(audit.source_label)}. Балл: {cell(audit.score)}.
              </p>
              {texts.length === 0 ? <p>Текста наблюдений нет.</p> : <ul>{texts.map((text) => <li key={cell(text)}>{cell(text)}</li>)}</ul>}
            </article>
          ) : (
            <p>Сохраненного балла нет.</p>
          )}
          <CompanyWorkspace workspace={workspace} action={action} />
          <h2>История проверок карточек</h2>
          <p className="hint">{typeof data.data.history_note === "string" ? data.data.history_note : ""}</p>
          {history.map((group) => (
            <section key={group.platform}>
              <h3>{group.title}</h3>
              {group.items.length === 0 ? (
                <p>{group.empty || "Проверок этой площадки в базе нет."}</p>
              ) : (
                group.items.map((item) => (
                  <details className="panel" key={item.id}>
                    <summary>
                      {item.when}. {item.source_label}. {item.verification_label}. {item.changes.join("; ")}
                    </summary>
                    <FactList facts={asFacts(item.facts)} contacts={asContacts(item.contacts)} platform={group.platform} />
                  </details>
                ))
              )}
            </section>
          ))}
          <details className="technical">
            <summary>Технические данные (для разработчика)</summary>
            <pre>{JSON.stringify(data.data.snapshots ?? [], null, 2)}</pre>
          </details>
        </>
      )}
    </section>
  );
}

type Action = {
  pending: boolean;
  message: string;
  error: string;
  run: (work: () => Promise<string>, confirmText?: string) => Promise<void>;
};

function IdentityPanel(props: { header: Row }): ReactNode {
  const identities = Array.isArray(props.header.platform_identities) ? (props.header.platform_identities as Row[]) : [];
  const missing = Array.isArray(props.header.missing_platforms) ? (props.header.missing_platforms as Row[]) : [];
  return (
    <article className="panel">
      <h2>Площадки и совпадение</h2>
      <p>
        Источник: {cell(props.header.source_platform_label)}. Совпадение: {cell(props.header.matching_status)}.
      </p>
      {identities.length === 0 ? (
        <p>Номера площадок пока не записаны.</p>
      ) : (
        <ul>
          {identities.map((item) => (
            <li key={`${cell(item.platform)}-${cell(item.external_id)}`}>
              {cell(item.label)}: {cell(item.external_id)}
            </li>
          ))}
        </ul>
      )}
      {missing.length > 0 ? (
        <p className="hint">Не проверялось: {missing.map((item) => cell(item.label)).join(", ")}.</p>
      ) : null}
      {props.header.review_needed === true ? <p>{cell(props.header.review_note)}</p> : null}
    </article>
  );
}

function asRows(value: unknown): Row[] {
  return Array.isArray(value) ? (value as Row[]) : [];
}

function LineList(props: { rows: unknown; empty: string }): ReactNode {
  const rows = Array.isArray(props.rows) ? props.rows.filter((item): item is string => typeof item === "string" && item.length > 0) : [];
  if (rows.length === 0) return <p>{props.empty}</p>;
  return (
    <ul>
      {rows.map((line) => (
        <li key={line}>{line}</li>
      ))}
    </ul>
  );
}

function CompanyWorkspace(props: { workspace: Row; action: Action }): ReactNode {
  const [pageFile, setPageFile] = useState<File | null>(null);
  const improvements = asRows(props.workspace.improvements);
  const prepare = (props.workspace.prepare || {}) as Row;
  const orders = asRows(prepare.orders);
  const prepareTasks = asRows(prepare.tasks);
  const materials = asRows(props.workspace.materials);
  const changes = asRows(props.workspace.changes);
  const approvals = (props.workspace.approvals || {}) as Row;
  const report = (props.workspace.report || {}) as Row;
  const pendingMaterials = asRows(approvals.materials);
  const humanTasks = asRows(approvals.human_tasks);
  const downloads = asRows(report.downloads);

  return (
    <div className="workspace">
      <h2>Что нужно улучшить</h2>
      {improvements.length === 0 ? (
        <p>Сохраненных замечаний по этой компании нет.</p>
      ) : (
        <ul>
          {improvements.map((item, index) => (
            <li key={`${cell(item.title)}-${index}`}>
              {cell(item.platform) && cell(item.platform) !== "нет" ? `${cell(item.platform)}. ` : ""}
              {cell(item.title)}. {cell(item.detail)}
            </li>
          ))}
        </ul>
      )}

      <h2>Подготовить улучшения</h2>
      <p className="hint">{cell(prepare.note)}</p>
      {prepareTasks.length === 0 ? (
        <p>Автоматических правок в сохраненном плане нет.</p>
      ) : (
        <LineList
          rows={prepareTasks.map((item) => {
            const place = cell(item.platform);
            const head = place !== "нет" ? `${place}. ` : "";
            return `${head}${cell(item.title)}. ${cell(item.detail)}`;
          })}
          empty="Автоматических правок в сохраненном плане нет."
        />
      )}
      {orders.length === 0 ? (
        <p>Заказа по этой компании нет, поэтому тексты и сухой прогон отсюда не запускаются.</p>
      ) : (
        orders.map((order) => (
          <article className="panel" key={cell(order.id)}>
            <p>
              Заказ. {cell(order.stage_label)}. Сумма {cell(order.amount_kzt)} тенге.
            </p>
            <div className="actions">
              <button
                type="button"
                disabled={props.action.pending}
                onClick={() =>
                  void props.action.run(
                    () =>
                      sendAction(`/api/v1/admin/orders/${cell(order.id)}/content/prepare`).then(
                        () => "Черновики текстов подготовлены из сохраненных данных. Ничего не опубликовано.",
                      ),
                    "Подготовить тексты по этому заказу? Публикации не будет.",
                  )
                }
              >
                Подготовить тексты
              </button>
              <button
                type="button"
                disabled={props.action.pending}
                onClick={() =>
                  void props.action.run(
                    () =>
                      sendAction(`/api/v1/admin/orders/${cell(order.id)}/listings/dry-run`, {}).then(
                        () => "Сухой прогон посчитал план. Форма кабинета не отправлялась.",
                      ),
                    "Сделать сухой прогон? Форма кабинета не отправляется.",
                  )
                }
              >
                Сухой прогон правок
              </button>
            </div>
          </article>
        ))
      )}

      <h2>Материалы и тексты</h2>
      {materials.length === 0 ? (
        <p>Сохраненных текстов и картинок нет.</p>
      ) : (
        materials.map((item) => (
          <article className="panel" key={cell(item.id)}>
            <p>
              {cell(item.title)}
              {cell(item.platform) !== "нет" ? `, ${cell(item.platform)}` : ""}. {cell(item.status_label)}. Версия {cell(item.version)}.
            </p>
            {typeof item.text === "string" && item.text ? <p>{item.text}</p> : <p>Текста в этой записи нет.</p>}
            {item.illustrative === true ? <p className="muted">Иллюстрация, не фотография компании.</p> : null}
          </article>
        ))
      )}

      <h2>Изменения карточек</h2>
      <p className="hint">Подготовка и симуляция читают сохраненную страницу кабинета. В живой кабинет ничего не уходит.</p>
      {changes.length === 0 ? (
        <p>Сохраненных правок карточек нет.</p>
      ) : (
        <>
          <label>
            Сохраненная страница кабинета (.html)
            <input type="file" accept=".html,text/html" onChange={(event) => setPageFile(event.target.files?.[0] || null)} />
          </label>
          {changes.map((change, index) => (
            <ChangeBlock key={`${cell(change.order_id)}-${index}`} change={change} pageFile={pageFile} action={props.action} />
          ))}
        </>
      )}

      <h2>Согласования</h2>
      {pendingMaterials.length === 0 && humanTasks.length === 0 ? (
        <p>Сейчас нет черновиков, которые ждут вашего утверждения.</p>
      ) : null}
      {pendingMaterials.map((item) => (
        <p key={cell(item.id)}>
          <button
            type="button"
            disabled={props.action.pending}
            onClick={() =>
              void props.action.run(
                () =>
                  sendAction(`/api/v1/admin/content/${cell(item.id)}/approve`, { version: item.version }).then(
                    () => `${cell(item.title)} утвержден. Это не публикация.`,
                  ),
                `Утвердить «${cell(item.title)}»? Текст на площадку не уйдет.`,
              )
            }
          >
            Утвердить: {cell(item.title)}
          </button>
        </p>
      ))}
      {asRows(approvals.listings).length > 0 ? (
        <p>Правки карточек, которые еще не утверждены, утверждаются в разделе «Изменения карточек» после выбора сохраненной страницы.</p>
      ) : null}
      {humanTasks.length > 0 && (
        <ul>
          {humanTasks.map((task) => (
            <li key={cell(task.id)}>
              {cell(task.title)}. {cell(task.status_label)}.
            </li>
          ))}
        </ul>
      )}

      <h2>Отчет по компании</h2>
      <h3>Сделанная работа</h3>
      <LineList rows={report.completed} empty="Завершенной работы в сохраненных данных нет." />
      <h3>Подготовленные изменения</h3>
      <LineList rows={report.prepared} empty="Подготовленных изменений нет." />
      <h3>Ждет утверждения</h3>
      <LineList rows={report.awaiting_approval} empty="Ничего не ждет утверждения." />
      <h3>Ждет модерации</h3>
      <LineList rows={report.moderation_pending} empty="На модерации ничего нет." />
      <h3>Не хватает сведений</h3>
      <LineList rows={report.missing} empty="Список недостающих сведений пуст." />
      {downloads.length === 0 ? (
        <p>Файлов отчета для скачивания нет.</p>
      ) : (
        downloads.map((item) => (
          <div className="actions" key={cell(item.id)}>
            {item.json === true ? (
              <button type="button" disabled={props.action.pending} onClick={() => void props.action.run(() => downloadAction(`/api/v1/admin/reports/${cell(item.id)}/completion.json`, "otchet.json"))}>
                Скачать JSON
              </button>
            ) : null}
            {item.html === true ? (
              <button type="button" disabled={props.action.pending} onClick={() => void props.action.run(() => downloadAction(`/api/v1/admin/reports/${cell(item.id)}/completion.html`, "otchet.html"))}>
                Скачать HTML
              </button>
            ) : null}
          </div>
        ))
      )}
      {orders.map((order) => (
        <button
          key={cell(order.id)}
          type="button"
          disabled={props.action.pending}
          onClick={() =>
            void props.action.run(
              () =>
                sendAction(`/api/v1/admin/orders/${cell(order.id)}/reports/completion`).then(
                  () => "Отчет собран из сохраненных данных. Карточка на площадке не открывалась.",
                ),
              "Собрать отчет из сохраненных данных? Карточка не откроется.",
            )
          }
        >
          Собрать отчет
        </button>
      ))}
      <Feedback message={props.action.message} error={props.action.error} />
    </div>
  );
}

function ChangeBlock(props: { change: Row; pageFile: File | null; action: Action }): ReactNode {
  const targets = asRows(props.change.targets);
  const items = asRows(props.change.items);
  const first = targets[0] || {};
  const [platform, setPlatform] = useState(typeof first.platform_code === "string" ? first.platform_code : "");
  const [operation, setOperation] = useState(typeof first.operation === "string" ? first.operation : "update");
  const fields = {
    package_id: cell(props.change.package_id),
    version: cell(props.change.version),
    platform,
    operation,
    mode: "offline",
  };
  return (
    <article className="panel">
      <p>
        Версия {cell(props.change.version)}. {cell(props.change.phase_label)}. Опубликовано: нет.
      </p>
      {items.length === 0 ? (
        <p>В этой версии нет отдельных полей.</p>
      ) : (
        <ul>
          {items.map((item, index) => (
            <li key={`${cell(item.field)}-${index}`}>
              {cell(item.platform)}, {cell(item.field)}: было {cell(item.before)}, станет {cell(item.after)}. {cell(item.status_label)}.
            </li>
          ))}
        </ul>
      )}
      {targets.length === 0 || !props.pageFile ? (
        <p className="hint">Кнопки подготовки, утверждения и симуляции появятся после выбора сохраненной страницы кабинета.</p>
      ) : (
        <div className="actions">
          <label>
            Площадка
            <select
              value={platform}
              onChange={(event) => {
                const value = event.target.value;
                setPlatform(value);
                const match = targets.find((item) => item.platform_code === value);
                if (match && typeof match.operation === "string") setOperation(match.operation);
              }}
            >
              {targets.map((item) => (
                <option key={cell(item.platform_code)} value={cell(item.platform_code)}>
                  {cell(item.platform)}
                </option>
              ))}
            </select>
          </label>
          <label>
            Действие
            <select value={operation} onChange={(event) => setOperation(event.target.value)}>
              <option value="update">обновление</option>
              <option value="create">создание</option>
            </select>
          </label>
          <button
            type="button"
            disabled={props.action.pending || !props.pageFile || !platform}
            onClick={() => {
              const chosen = props.pageFile;
              if (!chosen) return;
              void props.action.run(
                () =>
                  uploadAction(`/api/v1/admin/orders/${cell(props.change.order_id)}/listings/prepare`, chosen, fields).then(
                    () => "Офлайн-подготовка сохранена. Живой кабинет не открывался.",
                  ),
                "Подготовить эту площадку по сохраненной странице? Публикации не будет.",
              );
            }}
          >
            Подготовить эту площадку
          </button>
          <button
            type="button"
            disabled={props.action.pending || !props.pageFile || !platform}
            onClick={() => {
              const chosen = props.pageFile;
              if (!chosen) return;
              void props.action.run(
                () =>
                  uploadAction(`/api/v1/admin/orders/${cell(props.change.order_id)}/listings/approve`, chosen, fields).then(
                    () => "Версия утверждена для офлайн-симуляции. Это не публикация.",
                  ),
                "Утвердить эту версию только для выбранной площадки? Публикации не будет.",
              );
            }}
          >
            Утвердить эту версию
          </button>
          <button
            type="button"
            disabled={props.action.pending || !props.pageFile || !platform}
            onClick={() => {
              const chosen = props.pageFile;
              if (!chosen) return;
              void props.action.run(
                () =>
                  uploadAction(`/api/v1/admin/orders/${cell(props.change.order_id)}/listings/simulate`, chosen, fields).then(
                    () => "Офлайн-симуляция сохранена. В живой кабинет ничего не ушло.",
                  ),
                "Симулировать отправку офлайн? Карточка не будет опубликована.",
              );
            }}
          >
            Симулировать офлайн
          </button>
        </div>
      )}
    </article>
  );
}
