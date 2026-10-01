import {
  Activity,
  AlertCircle,
  ArrowDownToLine,
  ArrowUpRight,
  Building2,
  Check,
  ChevronDown,
  ChevronRight,
  CircleHelp,
  Database,
  FileSearch,
  LoaderCircle,
  Search,
  ShieldAlert,
  Users,
  X,
} from "lucide-react";
import { useDeferredValue, useEffect, useState, type FormEvent } from "react";

type DatasetStats = {
  counts: {
    lots: number;
    lots_with_items: number;
    items: number;
    participations: number;
    suppliers: number;
  };
  coverage: {
    lots_with_items_percent: number;
    lots_with_participations_percent: number;
  };
  last_publish_date: string | null;
  data_version: string | null;
  registry_sources: { msp: string; rnp: string };
};

type LotItem = {
  pos: number;
  product_name: string;
  okpd2_code: string;
  weight: number;
};

type Lot = {
  lot_id: string;
  publish_date: string | null;
  start_price: number | null;
  procedure_name: string;
  subject: string;
  is_smp: number | null;
  customer_inn: string | null;
  customer_kpp: string | null;
  items: LotItem[];
};

type Evidence = {
  lot_id: string;
  publish_date: string | null;
  title: string;
  okpd2: string[];
  item_coverage?: number;
  text_similarity?: number;
  customer_inn: string | null;
  price: number | null;
};

type EnrichmentFact = {
  status: string;
  source: string;
  source_url: string;
  checked_at: string | null;
  name?: string | null;
  category?: number | null;
  okved?: string | null;
  region?: string | null;
  included_at?: string | null;
};

type Recommendation = {
  rank: number;
  inn: string;
  name: string | null;
  score: number;
  role: string;
  role_conf: number;
  role_source: string;
  role_signals: string[];
  status: string;
  msp: boolean;
  msp_status: "member" | "not_member" | "unknown";
  risk: boolean;
  risk_status: "listed" | "clear" | "unknown";
  factor_scores: Record<string, number>;
  factors: Record<string, number>;
  evidence: Evidence[];
  explain: string;
  enrichment: { msp: EnrichmentFact; rnp: EnrichmentFact };
};

type RecommendationResponse = {
  lot_id: string;
  run_id: string;
  took_ms: number;
  data_version: string;
  lot: Omit<Lot, "items">;
  items: Recommendation[];
  registry_checks: {
    msp: string;
    msp_candidate_search: string;
    rnp: string;
    errors: Record<string, string>;
  };
};

type SupplierProfile = {
  inn: string;
  name: string | null;
  is_msp: boolean;
  msp_status: string;
  risk_status: string;
  role: string;
  role_conf: number;
  role_source: string;
  role_signals: string[];
  region: string | null;
  stats: { lots: number; wins: number; win_rate: number | null; categories: number };
  categories: Array<{ okpd2: string; wins: number }>;
  history: Array<{
    lot_id: string;
    publish_date: string | null;
    title: string;
    okpd2: string[];
    is_winner: boolean;
    price: number | null;
  }>;
  enrichment: { msp: EnrichmentFact; rnp: EnrichmentFact };
};

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const FACTOR_LABELS: Record<string, string> = {
  F1: "Опыт по позициям",
  F2: "Текстовое сходство",
  F3: "История побед",
  F4: "Опыт у заказчика",
  F5: "Ценовой диапазон",
  F6: "Давность победы",
  F7: "Регион",
  F8: "МСП",
};

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, init);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      // Keep the HTTP status when the error response is not JSON.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

function formatNumber(value: number | null | undefined): string {
  if (value == null) return "—";
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(value);
}

function formatMoney(value: number | null | undefined): string {
  if (value == null) return "Не указана";
  return new Intl.NumberFormat("ru-RU", {
    maximumFractionDigits: 0,
    style: "currency",
    currency: "RUB",
  }).format(value);
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf())
    ? value
    : new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium" }).format(parsed);
}

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    member: "В реестре МСП",
    not_member: "Не найден в МСП",
    listed: "Найден в РНП",
    clear: "В РНП не найден",
    unknown: "Не проверен",
    "Проверенный": "Проверенный",
    "Участник": "Участник",
    "Новый (реестр МСП)": "Новый из реестра",
    "Риск": "Риск",
  };
  return labels[status] ?? status;
}

function AppMark() {
  return (
    <div className="app-mark" aria-label="Контур закупок">
      <span className="app-mark__glyph">К</span>
      <span className="app-mark__copy">
        <strong>КОНТУР ЗАКУПОК</strong>
        <small>АНАЛИТИКА ПОСТАВЩИКОВ</small>
      </span>
    </div>
  );
}

function SourceStatus({ status, source }: { status: string; source: string }) {
  const available = status === "available";
  return (
    <span className={`source-status ${available ? "is-available" : "is-unavailable"}`}>
      <span className="source-status__dot" />
      {source}: {available ? "на связи" : "не проверен"}
    </span>
  );
}

function RegistryFact({ title, fact }: { title: string; fact: EnrichmentFact }) {
  return (
    <section className="registry-fact">
      <div className="registry-fact__heading">
        <span>{title}</span>
        <span className={`state-pill state-pill--${fact.status}`}>
          {statusLabel(fact.status)}
        </span>
      </div>
      {fact.name && <strong className="registry-fact__name">{fact.name}</strong>}
      <div className="registry-fact__meta">
        {fact.category != null && <span>Категория МСП: {fact.category}</span>}
        {fact.okved && <span>ОКВЭД: {fact.okved}</span>}
        {fact.region && <span>Регион: {fact.region}</span>}
        {fact.included_at && <span>Включён: {formatDate(fact.included_at)}</span>}
      </div>
      <div className="registry-fact__source">
        <span>{fact.source}</span>
        <span>Проверено: {formatDate(fact.checked_at)}</span>
        {fact.source_url && (
          <a href={fact.source_url} target="_blank" rel="noreferrer" aria-label={`Открыть ${title}`}>
            <ArrowUpRight size={14} />
          </a>
        )}
      </div>
    </section>
  );
}

function App() {
  const [stats, setStats] = useState<DatasetStats | null>(null);
  const [statsError, setStatsError] = useState<string | null>(null);
  const [lotId, setLotId] = useState("4652711");
  const [lot, setLot] = useState<Lot | null>(null);
  const [recommendation, setRecommendation] = useState<RecommendationResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tableSearch, setTableSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("Все роли");
  const [statusFilter, setStatusFilter] = useState("Все статусы");
  const [expandedInn, setExpandedInn] = useState<string | null>(null);
  const [profile, setProfile] = useState<SupplierProfile | null>(null);
  const [profileLoading, setProfileLoading] = useState(false);
  const [profileError, setProfileError] = useState<string | null>(null);
  const deferredSearch = useDeferredValue(tableSearch.trim().toLowerCase());

  useEffect(() => {
    let active = true;
    requestJson<DatasetStats>("/api/v1/stats")
      .then((result) => {
        if (active) setStats(result);
      })
      .catch((loadError: unknown) => {
        if (active) setStatsError(loadError instanceof Error ? loadError.message : "Не удалось загрузить статистику");
      });
    return () => {
      active = false;
    };
  }, []);

  async function handleSearch(event?: FormEvent<HTMLFormElement>): Promise<void> {
    event?.preventDefault();
    const requestedLotId = lotId.trim();
    if (!requestedLotId) return;
    setLoading(true);
    setError(null);
    setProfile(null);
    setProfileError(null);
    setExpandedInn(null);
    try {
      const [result, details] = await Promise.all([
        requestJson<RecommendationResponse>("/api/v1/recommendations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lot_id: requestedLotId, top_k: 20 }),
        }),
        requestJson<Lot>(`/api/v1/lots/${encodeURIComponent(requestedLotId)}`),
      ]);
      setRecommendation(result);
      setLot(details);
    } catch (searchError: unknown) {
      setRecommendation(null);
      setLot(null);
      setError(searchError instanceof Error ? searchError.message : "Не удалось рассчитать рекомендации");
    } finally {
      setLoading(false);
    }
  }

  async function openSupplier(inn: string): Promise<void> {
    if (expandedInn === inn) {
      setExpandedInn(null);
      return;
    }
    setExpandedInn(inn);
    setProfile(null);
    setProfileError(null);
    setProfileLoading(true);
    try {
      setProfile(await requestJson<SupplierProfile>(`/api/v1/suppliers/${encodeURIComponent(inn)}`));
    } catch (profileLoadError: unknown) {
      setProfileError(profileLoadError instanceof Error ? profileLoadError.message : "Не удалось загрузить профиль");
    } finally {
      setProfileLoading(false);
    }
  }

  async function exportResult(): Promise<void> {
    if (!recommendation?.run_id) return;
    try {
      const response = await fetch(
        `${API_BASE}/api/v1/batch/runs/${encodeURIComponent(recommendation.run_id)}/export?format=xlsx`,
      );
      if (!response.ok) throw new Error("Не удалось сформировать XLSX");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `recommendations_${recommendation.run_id}.xlsx`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (exportError: unknown) {
      setError(exportError instanceof Error ? exportError.message : "Не удалось скачать результат");
    }
  }

  const filteredItems = (recommendation?.items ?? []).filter((item) => {
    const matchesText =
      !deferredSearch ||
      item.inn.toLowerCase().includes(deferredSearch) ||
      (item.name ?? "").toLowerCase().includes(deferredSearch);
    const matchesRole = roleFilter === "Все роли" || item.role === roleFilter;
    const matchesStatus = statusFilter === "Все статусы" || item.status === statusFilter;
    return matchesText && matchesRole && matchesStatus;
  });

  return (
    <div className="procurement-app">
      <header className="topbar">
        <AppMark />
        <div className="topbar__right">
          <span className="topbar__context">Санкт-Петербург · закупки 2024–2025</span>
          <span className="topbar__user" title="Аналитик">А</span>
        </div>
      </header>

      <main className="workspace">
        <div className="page-heading">
          <div>
            <div className="eyebrow">АНАЛИТИКА ПОСТАВЩИКОВ</div>
            <h1>Подбор контрагентов</h1>
            <p>История закупок, похожесть и подтверждённые источники</p>
          </div>
          <div className="page-heading__status">
            <span className={`connection-mark ${stats ? "is-ready" : ""}`} />
            {stats ? "База подключена" : "Подключение к API"}
          </div>
        </div>

        <section className="search-panel" aria-label="Поиск закупки">
          <form className="lot-search" onSubmit={handleSearch}>
            <label htmlFor="lot-id">ID лота</label>
            <div className="lot-search__control">
              <FileSearch size={18} aria-hidden="true" />
              <input
                id="lot-id"
                value={lotId}
                onChange={(event) => setLotId(event.target.value)}
                placeholder="Например, 4652711"
                inputMode="numeric"
              />
              <button type="submit" className="primary-button" disabled={loading || !lotId.trim()}>
                {loading ? <LoaderCircle className="spin" size={17} /> : <Search size={17} />}
                {loading ? "Подбираем" : "Подобрать"}
              </button>
            </div>
          </form>
          <div className="search-panel__hint">
            <CircleHelp size={14} />
            Расчёт может занять дольше для лотов с большим количеством позиций
          </div>
        </section>

        <section className="metrics-strip" aria-label="Статистика базы">
          <div className="metric-cell">
            <span className="metric-cell__label"><Database size={15} /> Лоты</span>
            <strong>{formatNumber(stats?.counts.lots)}</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-cell__label"><FileSearch size={15} /> Позиции ТРУ</span>
            <strong>{formatNumber(stats?.counts.items)}</strong>
          </div>
          <div className="metric-cell">
            <span className="metric-cell__label"><Users size={15} /> Поставщики</span>
            <strong>{formatNumber(stats?.counts.suppliers)}</strong>
          </div>
          <div className="metric-cell metric-cell--wide">
            <span className="metric-cell__label"><Activity size={15} /> Последняя закупка</span>
            <strong>{formatDate(stats?.last_publish_date)}</strong>
          </div>
          <div className="metric-cell metric-cell--sources">
            <SourceStatus status={recommendation?.registry_checks.msp ?? "unavailable"} source="ФНС · МСП" />
            <SourceStatus status={recommendation?.registry_checks.rnp ?? "unavailable"} source="ЕИС · РНП" />
          </div>
        </section>

        {statsError && (
          <div className="inline-alert" role="status">
            <AlertCircle size={17} /> Статистика недоступна: {statsError}
          </div>
        )}
        {error && (
          <div className="inline-alert inline-alert--error" role="alert">
            <AlertCircle size={17} /> {error}
          </div>
        )}

        {lot && recommendation && (
          <>
            <section className="lot-summary" aria-label="Целевая закупка">
              <div className="lot-summary__main">
                <div className="lot-summary__meta">
                  <span className="lot-id">ЛОТ {lot.lot_id}</span>
                  <span>{formatDate(lot.publish_date)}</span>
                  {lot.is_smp === 1 && <span className="tag tag--msp">Закупка МСП</span>}
                </div>
                <h2>{lot.procedure_name || lot.subject || "Закупка без названия"}</h2>
                {lot.subject && lot.subject !== lot.procedure_name && <p>{lot.subject}</p>}
              </div>
              <dl className="lot-summary__facts">
                <div><dt>НМЦК</dt><dd>{formatMoney(lot.start_price)}</dd></div>
                <div><dt>Позиции</dt><dd>{formatNumber(lot.items.length)}</dd></div>
                <div><dt>ИНН заказчика</dt><dd>{lot.customer_inn || "Не указан"}</dd></div>
              </dl>
            </section>

            <section className="results-section" aria-label="Рекомендации">
              <div className="results-heading">
                <div>
                  <div className="eyebrow">РАНЖИРОВАНИЕ</div>
                  <h2>Рекомендованные контрагенты <span>{filteredItems.length}</span></h2>
                </div>
                <div className="results-heading__actions">
                  <span className="elapsed-time">{(recommendation.took_ms / 1000).toFixed(1)} с</span>
                  <button className="secondary-button" onClick={exportResult}>
                    <ArrowDownToLine size={16} /> Экспорт XLSX
                  </button>
                </div>
              </div>

              <div className="filter-row">
                <label className="table-search">
                  <Search size={16} />
                  <input
                    value={tableSearch}
                    onChange={(event) => setTableSearch(event.target.value)}
                    placeholder="Поиск по ИНН или названию"
                    aria-label="Поиск по рекомендациям"
                  />
                  {tableSearch && (
                    <button type="button" aria-label="Очистить поиск" onClick={() => setTableSearch("")}>
                      <X size={14} />
                    </button>
                  )}
                </label>
                <label className="select-filter">
                  <span>Роль</span>
                  <select value={roleFilter} onChange={(event) => setRoleFilter(event.target.value)}>
                    <option>Все роли</option>
                    <option>производитель</option>
                    <option>дистрибьютор</option>
                    <option>поставщик</option>
                    <option>не определена</option>
                  </select>
                  <ChevronDown size={14} />
                </label>
                <label className="select-filter">
                  <span>Статус</span>
                  <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
                    <option>Все статусы</option>
                    <option>Проверенный</option>
                    <option>Участник</option>
                    <option>Новый (реестр МСП)</option>
                    <option>Риск</option>
                  </select>
                  <ChevronDown size={14} />
                </label>
              </div>

              <div className="table-wrap">
                <table className="recommendation-table">
                  <thead>
                    <tr>
                      <th className="rank-column">№</th>
                      <th>Контрагент</th>
                      <th>Роль</th>
                      <th>Совпадение</th>
                      <th>МСП</th>
                      <th>РНП</th>
                      <th>Оценка</th>
                      <th aria-label="Подробности" />
                    </tr>
                  </thead>
                  <tbody>
                    {filteredItems.map((item) => {
                      const expanded = expandedInn === item.inn;
                      return (
                        <FragmentRow
                          key={item.inn}
                          item={item}
                          expanded={expanded}
                          onToggle={() => void openSupplier(item.inn)}
                          loading={profileLoading && expanded}
                          profile={expanded ? profile : null}
                          profileError={expanded ? profileError : null}
                        />
                      );
                    })}
                    {filteredItems.length === 0 && (
                      <tr><td colSpan={8} className="empty-row">Подходящих строк нет</td></tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div className="results-footer">
                <span>Версия данных {recommendation.data_version}</span>
                <span>Поиск: точный ОКПД2 → родственные коды → текст</span>
              </div>
            </section>
          </>
        )}

        {!lot && !recommendation && !loading && !error && (
          <section className="empty-state">
            <div className="empty-state__icon"><Search size={22} /></div>
            <h2>Введите ID лота для подбора</h2>
            <p>Сервис сравнит закупку с историей торгов и покажет доказательства по каждому кандидату.</p>
          </section>
        )}

        {recommendation && Object.keys(recommendation.registry_checks.errors).length > 0 && (
          <div className="source-notice">
            <ShieldAlert size={16} />
            Некоторые статусы источников не удалось проверить. Они отмечены как «Не проверен» и не трактуются как отсутствие записи.
          </div>
        )}
      </main>
      <footer className="app-footer">
        <span>Ранжирование по истории закупок и открытым источникам</span>
        <span>Без внешних API нейронных сетей</span>
      </footer>
    </div>
  );
}

function FragmentRow({
  item,
  expanded,
  onToggle,
  loading,
  profile,
  profileError,
}: {
  item: Recommendation;
  expanded: boolean;
  onToggle: () => void;
  loading: boolean;
  profile: SupplierProfile | null;
  profileError: string | null;
}) {
  return (
    <>
      <tr className={`recommendation-row ${expanded ? "is-expanded" : ""}`}>
        <td className="rank-column"><span className={item.rank <= 3 ? "rank-number is-top" : "rank-number"}>{item.rank}</span></td>
        <td>
          <button className="supplier-cell" onClick={onToggle} aria-expanded={expanded}>
            <span className="supplier-cell__name">{item.name || "Поставщик из истории закупок"}</span>
            <span className="supplier-cell__inn">ИНН {item.inn}</span>
          </button>
        </td>
        <td>
          <span className={`role-pill role-pill--${roleKey(item.role)}`}>{item.role}</span>
          <span className="subvalue">Уверенность {Math.round(item.role_conf * 100)}%</span>
        </td>
        <td>
          <div className="match-cell">
            <span className="match-cell__value">{Math.round((item.factor_scores.F1 ?? 0) * 100)}%</span>
            <span className="match-meter"><span style={{ width: `${Math.round((item.factor_scores.F1 ?? 0) * 100)}%` }} /></span>
          </div>
        </td>
        <td><span className={`state-pill state-pill--${item.msp_status}`}>{statusLabel(item.msp_status)}</span></td>
        <td><span className={`state-pill state-pill--${item.risk_status}`}>{statusLabel(item.risk_status)}</span></td>
        <td><strong className="score-value">{item.score.toFixed(1)}</strong></td>
        <td><button className="expand-button" onClick={onToggle} aria-label={expanded ? "Скрыть подробности" : "Показать подробности"}>{expanded ? <ChevronDown size={17} /> : <ChevronRight size={17} />}</button></td>
      </tr>
      {expanded && (
        <tr className="detail-row">
          <td colSpan={8}>
            <div className="detail-grid">
              <section className="detail-pane">
                <div className="detail-pane__title"><Building2 size={16} /> Основания классификации</div>
                <p className="detail-source">{item.role_source}</p>
                <ul className="signal-list">{item.role_signals.map((signal) => <li key={signal}>{signal}</li>)}</ul>
                <p className="explanation-copy">{item.explain}</p>
                <div className="factor-list">
                  {Object.entries(item.factor_scores).map(([factor, value]) => (
                    <div className="factor-line" key={factor}>
                      <span>{FACTOR_LABELS[factor] ?? factor}</span>
                      <span className="factor-line__bar"><span style={{ width: `${Math.round(value * 100)}%` }} /></span>
                      <strong>{Math.round(value * 100)}%</strong>
                    </div>
                  ))}
                </div>
              </section>
              <section className="detail-pane detail-pane--wide">
                <div className="detail-pane__title"><Activity size={16} /> Доказательства и реестры</div>
                {item.evidence.length ? (
                  <div className="evidence-list">
                    {item.evidence.map((evidence) => (
                      <article className="evidence-item" key={evidence.lot_id}>
                        <div className="evidence-item__top">
                          <a href={`/api/v1/lots/${encodeURIComponent(evidence.lot_id)}`} target="_blank" rel="noreferrer">
                            Лот {evidence.lot_id} <ArrowUpRight size={13} />
                          </a>
                          <span>{formatDate(evidence.publish_date)}</span>
                        </div>
                        <p>{evidence.title || "Закупка без названия"}</p>
                        <div className="evidence-item__tags">
                          {evidence.okpd2.map((code) => <span key={code}>{code}</span>)}
                          {evidence.item_coverage != null && <span>Покрытие {Math.round(evidence.item_coverage * 100)}%</span>}
                          {evidence.text_similarity != null && <span>Текст {Math.round(evidence.text_similarity * 100)}%</span>}
                        </div>
                      </article>
                    ))}
                  </div>
                ) : <p className="muted-copy">Подходящие победы в истории не найдены.</p>}
                <div className="registry-facts">
                  <RegistryFact title="Малый и средний бизнес" fact={item.enrichment.msp} />
                  <RegistryFact title="Реестр недобросовестных поставщиков" fact={item.enrichment.rnp} />
                </div>
                <button className="text-action" onClick={onToggle}>
                  {loading ? <LoaderCircle className="spin" size={15} /> : <Users size={15} />}
                  {loading ? "Загружаем историю" : "Карточка поставщика"}
                </button>
                {profileError && <p className="profile-error">{profileError}</p>}
                {profile && (
                  <div className="profile-summary">
                    <strong>{profile.name || `ИНН ${profile.inn}`}</strong>
                    <span>{profile.stats.lots} участий · {profile.stats.wins} побед · роль: {profile.role}</span>
                    <span>Роль определена по: {profile.role_source}; сигналы: {profile.role_signals.join(", ")}</span>
                    {profile.history.slice(0, 3).map((event) => (
                      <span className="profile-history" key={event.lot_id}>
                        {formatDate(event.publish_date)} · {event.title} · {event.is_winner ? "победа" : "участие"}
                      </span>
                    ))}
                  </div>
                )}
              </section>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function roleKey(role: string): string {
  if (role === "производитель") return "manufacturer";
  if (role === "дистрибьютор") return "distributor";
  if (role === "поставщик") return "supplier";
  return "unknown";
}

export default App;