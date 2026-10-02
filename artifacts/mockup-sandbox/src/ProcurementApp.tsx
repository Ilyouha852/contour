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
  FileText,
  Hash,
  LoaderCircle,
  Search,
  ShieldAlert,
  TrendingDown,
  Users,
  X,
} from "lucide-react";
import { useDeferredValue, useEffect, useState, type FormEvent } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

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

type DatasetBackupStatus = {
  available: boolean;
  created_at?: string;
  data_version?: string;
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

type LotSearchResult = Omit<Lot, "items" | "customer_kpp"> & {
  item_count: number;
  matched_terms: number;
  match_ratio: number;
};

type LotSearchSuggestion = {
  phrase: string;
  reason: string;
  matched_lots: number;
  okpd2_codes: string[];
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

type FactorEvidence = {
  lot_id?: string;
  publish_date?: string | null;
  title?: string;
  is_winner?: boolean;
  item_coverage?: number;
  text_similarity?: number;
  recency_weight?: number;
  retrieval_weight?: number;
  contribution?: number;
  days_old?: number;
  price?: number | null;
};

type FactorDetail = {
  formula: string;
  weight: number;
  weighted_contribution: number;
  final_contribution: number;
  inputs: Array<{ label: string; value: string | number | boolean | null }>;
  evidence: FactorEvidence[];
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
  factor_details?: Record<string, FactorDetail>;
  score_limitations?: Array<{
    factor: string;
    score: number;
    lost_points: number;
    reason: string;
  }>;
  score_modifier?: { value: number; lost_points: number; reason: string };
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
const FACTOR_EXPLANATIONS: Record<string, string> = {
  F1: "Суммирует опыт в похожих закупках по каждой позиции лота. Победы весят больше проигранных участий, а старые результаты постепенно теряют влияние.",
  F2: "Сравнивает текст закупки с выигранными лотами. Редкие совпавшие слова ценнее общих; результат нормируется по найденной текстовой выборке.",
  F3: "Показывает долю побед среди похожих участий. Сглаживание α и базовая доля побед не дают единичному случаю резко изменить оценку.",
  F4: "Учитывает победы в похожих закупках именно у этого заказчика. Несколько подтверждённых побед повышают значение, но эффект постепенно насыщается.",
  F5: "Сравнивает начальную цену целевого лота с медианой цен похожих побед. Важна относительная близость цен, а не разница только в рублях.",
  F6: "Оценивает свежесть последней победы в похожей закупке: чем больше прошло времени, тем меньше вклад.",
  F7: "Сопоставляет регион поставщика по КПП с регионом заказчика. При нехватке региональных данных используется базовая оценка.",
  F8: "Для закупки с ограничением МСП статус из реестра даёт полную, нулевую или промежуточную оценку. Для обычной закупки этот критерий не снижает балл.",
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

function formatFactorValue(value: string | number | boolean | null): string {
  if (value == null || value === "") return "—";
  if (typeof value === "boolean") return value ? "Да" : "Нет";
  if (typeof value === "string") return value;
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 4 }).format(value);
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
  const [searchMode, setSearchMode] = useState<"id" | "keyword">("id");
  const [lotSearchResults, setLotSearchResults] = useState<LotSearchResult[]>([]);
  const [lotSearchSuggestions, setLotSearchSuggestions] = useState<LotSearchSuggestion[]>([]);
  const [lotSearchComplete, setLotSearchComplete] = useState(false);
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
  const [selectedLotId, setSelectedLotId] = useState<string | null>(null);
  const [selectedLot, setSelectedLot] = useState<Lot | null>(null);
  const [selectedLotLoading, setSelectedLotLoading] = useState(false);
  const [selectedLotError, setSelectedLotError] = useState<string | null>(null);
  const [datasetDialogOpen, setDatasetDialogOpen] = useState(false);
  const [noticesCsv, setNoticesCsv] = useState<File | null>(null);
  const [itemsCsv, setItemsCsv] = useState<File | null>(null);
  const [suppliersCsv, setSuppliersCsv] = useState<File | null>(null);
  const [adminToken, setAdminToken] = useState("");
  const [datasetAction, setDatasetAction] = useState<"replace" | "rollback" | null>(null);
  const [datasetMessage, setDatasetMessage] = useState<string | null>(null);
  const [datasetError, setDatasetError] = useState<string | null>(null);
  const [backupStatus, setBackupStatus] = useState<DatasetBackupStatus>({ available: false });
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
    requestJson<DatasetBackupStatus>("/api/v1/datasets/backup")
      .then((result) => {
        if (active) setBackupStatus(result);
      })
      .catch(() => {
        if (active) setBackupStatus({ available: false });
      });
    return () => {
      active = false;
    };
  }, []);

  async function refreshDatasetStatus(): Promise<void> {
    const [latestStats, latestBackup] = await Promise.all([
      requestJson<DatasetStats>("/api/v1/stats"),
      requestJson<DatasetBackupStatus>("/api/v1/datasets/backup"),
    ]);
    setStats(latestStats);
    setStatsError(null);
    setBackupStatus(latestBackup);
  }

  async function replaceDataset(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!noticesCsv || !itemsCsv || !suppliersCsv) {
      setDatasetError("Выберите все три CSV-файла перед заменой базы.");
      return;
    }
    setDatasetAction("replace");
    setDatasetError(null);
    setDatasetMessage(null);
    const formData = new FormData();
    formData.append("notices_file", noticesCsv);
    formData.append("items_file", itemsCsv);
    formData.append("suppliers_file", suppliersCsv);
    const headers = adminToken.trim() ? { "X-Admin-Token": adminToken.trim() } : undefined;
    try {
      const response = await fetch(`${API_BASE}/api/v1/datasets/import`, {
        method: "POST",
        headers,
        body: formData,
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
        throw new Error(detail || `${response.status} ${response.statusText}`);
      }
      await refreshDatasetStatus();
      setLot(null);
      setRecommendation(null);
      setLotSearchResults([]);
      setLotSearchComplete(false);
      setDatasetMessage(
        `База заменена. Загружено ${formatNumber(body.counts?.notices)} лотов и ${formatNumber(body.counts?.items)} позиций. Откат к предыдущей версии доступен.`,
      );
      setNoticesCsv(null);
      setItemsCsv(null);
      setSuppliersCsv(null);
    } catch (uploadError: unknown) {
      setDatasetError(uploadError instanceof Error ? uploadError.message : "Не удалось заменить базу данных");
    } finally {
      setDatasetAction(null);
    }
  }

  async function rollbackDataset(): Promise<void> {
    if (!backupStatus.available || datasetAction) return;
    if (!window.confirm("Восстановить базу из снимка, созданного перед последней заменой? Текущие данные будут заменены.")) return;
    setDatasetAction("rollback");
    setDatasetError(null);
    setDatasetMessage(null);
    const headers = adminToken.trim() ? { "X-Admin-Token": adminToken.trim() } : undefined;
    try {
      const response = await fetch(`${API_BASE}/api/v1/datasets/rollback`, { method: "POST", headers });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
        throw new Error(detail || `${response.status} ${response.statusText}`);
      }
      await refreshDatasetStatus();
      setLot(null);
      setRecommendation(null);
      setLotSearchResults([]);
      setLotSearchComplete(false);
      setDatasetMessage(`Предыдущая версия базы восстановлена (${body.restored_data_version ?? "версия неизвестна"}).`);
    } catch (rollbackError: unknown) {
      setDatasetError(rollbackError instanceof Error ? rollbackError.message : "Не удалось откатить базу данных");
    } finally {
      setDatasetAction(null);
    }
  }

  async function handleSearch(event?: FormEvent<HTMLFormElement>): Promise<void> {
    event?.preventDefault();
    const query = lotId.trim();
    if (!query || (searchMode === "keyword" && query.length < 2)) return;

    setError(null);
    if (searchMode === "keyword") {
      await searchKeyword(query);
      return;
    }

    setLotSearchResults([]);
    setLotSearchComplete(false);
    await runRecommendation(query);
  }

  async function searchKeyword(query: string): Promise<void> {
    setLoading(true);
    setLotSearchComplete(false);
    setLotSearchSuggestions([]);
    setLot(null);
    setRecommendation(null);
    setExpandedInn(null);
    setProfile(null);
    try {
      const result = await requestJson<{
        count: number;
        items: LotSearchResult[];
        suggestions: LotSearchSuggestion[];
      }>(`/api/v1/lots/search?q=${encodeURIComponent(query)}&limit=20`);
      setLotSearchResults(result.items);
      setLotSearchSuggestions(result.suggestions ?? []);
      setLotSearchComplete(true);
    } catch (searchError: unknown) {
      setLotSearchResults([]);
      setLotSearchSuggestions([]);
      setLotSearchComplete(false);
      setError(searchError instanceof Error ? searchError.message : "Не удалось найти лоты");
    } finally {
      setLoading(false);
    }
  }

  function searchSuggestedPhrase(phrase: string): void {
    setLotId(phrase);
    setSearchMode("keyword");
    void searchKeyword(phrase);
  }

  async function runRecommendation(requestedLotId: string): Promise<void> {
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

  function selectLot(result: LotSearchResult): void {
    setLotId(result.lot_id);
    setSearchMode("id");
    setLotSearchResults([]);
    setLotSearchComplete(false);
    void runRecommendation(result.lot_id);
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

  async function openEvidenceLot(lotId: string): Promise<void> {
    setSelectedLotId(lotId);
    setSelectedLot(null);
    setSelectedLotError(null);
    setSelectedLotLoading(true);
    try {
      setSelectedLot(await requestJson<Lot>(`/api/v1/lots/${encodeURIComponent(lotId)}`));
    } catch (lotLoadError: unknown) {
      setSelectedLotError(lotLoadError instanceof Error ? lotLoadError.message : "Не удалось загрузить лот");
    } finally {
      setSelectedLotLoading(false);
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
        <button
          className="secondary-button dataset-open-button"
          type="button"
          onClick={() => {
            setDatasetError(null);
            setDatasetMessage(null);
            setDatasetDialogOpen(true);
          }}
        >
          <Database size={15} /> Данные
        </button>
      </header>

      <main className="workspace">
        <div className="page-heading">
          <div>
            <div className="eyebrow">АНАЛИТИКА ПОСТАВЩИКОВ</div>
            <h1>Подбор контрагентов</h1>
            <p>История закупок, похожесть и подтверждённые источники</p>
          </div>
        </div>

        <section className="search-panel" aria-label="Поиск закупки">
          <form className="lot-search" onSubmit={handleSearch}>
            <div className="search-mode" role="group" aria-label="Режим поиска">
              <button
                type="button"
                className={searchMode === "id" ? "search-mode__option is-active" : "search-mode__option"}
                aria-pressed={searchMode === "id"}
                onClick={() => {
                  setSearchMode("id");
                  setLotId("");
                  setLotSearchComplete(false);
                  setLotSearchResults([]);
                  setLotSearchSuggestions([]);
                }}
              >
                <Hash size={14} /> ID лота
              </button>
              <button
                type="button"
                className={searchMode === "keyword" ? "search-mode__option is-active" : "search-mode__option"}
                aria-pressed={searchMode === "keyword"}
                onClick={() => {
                  setSearchMode("keyword");
                  setLotId("");
                  setLotSearchComplete(false);
                  setLotSearchResults([]);
                  setLotSearchSuggestions([]);
                }}
              >
                <Search size={14} /> Ключевое слово
              </button>
            </div>
            <label htmlFor="lot-id">{searchMode === "id" ? "ID лота" : "Ключевое слово или фраза"}</label>
            <div className="lot-search__control">
              {searchMode === "id" ? <FileSearch size={18} aria-hidden="true" /> : <Search size={18} aria-hidden="true" />}
              <input
                id="lot-id"
                value={lotId}
                onChange={(event) => setLotId(event.target.value)}
                placeholder={searchMode === "id" ? "Например, 4652711" : "Например, обслуживание бассейнов"}
                inputMode={searchMode === "id" ? "numeric" : "search"}
              />
              <button
                type="submit"
                className="primary-button"
                disabled={loading || !lotId.trim() || (searchMode === "keyword" && lotId.trim().length < 2)}
              >
                {loading ? <LoaderCircle className="spin" size={17} /> : <Search size={17} />}
                {loading ? (searchMode === "keyword" ? "Ищем" : "Подбираем") : (searchMode === "keyword" ? "Найти лоты" : "Подобрать")}
              </button>
            </div>
          </form>
          <div className="search-panel__hint">
            <CircleHelp size={14} />
            {searchMode === "keyword"
              ? "Ищите по названию, предмету закупки или позициям ТРУ"
              : "Расчёт может занять дольше для лотов с большим количеством позиций"}
          </div>
        </section>

        {searchMode === "keyword" && lotSearchComplete && (
          <section className="lot-search-results" aria-label="Результаты поиска лотов">
            <div className="lot-search-results__heading">
              <div>
                <div className="eyebrow">ПОИСК ПО КЛЮЧЕВЫМ СЛОВАМ</div>
                <h2>Найденные закупки <span>{lotSearchResults.length}</span></h2>
              </div>
              <span>«{lotId.trim()}»</span>
            </div>
            {lotSearchResults.length ? (
              <div className="lot-search-results__list">
                {lotSearchResults.map((result) => (
                  <button
                    type="button"
                    className="lot-search-result"
                    key={result.lot_id}
                    onClick={() => selectLot(result)}
                    disabled={loading}
                  >
                    <span className="lot-search-result__main">
                      <span className="lot-search-result__meta">
                        <strong>ЛОТ {result.lot_id}</strong>
                        <span>{formatDate(result.publish_date)}</span>
                      </span>
                      <span className="lot-search-result__title">
                        {result.procedure_name || result.subject || "Закупка без названия"}
                      </span>
                      {result.subject && result.subject !== result.procedure_name && (
                        <span className="lot-search-result__subject">{result.subject}</span>
                      )}
                    </span>
                    <span className="lot-search-result__facts">
                      <span>{Math.round(result.match_ratio * 100)}% слов</span>
                      <span>{result.item_count} поз.</span>
                      <span>{formatMoney(result.start_price)}</span>
                    </span>
                    <ChevronRight size={17} />
                  </button>
                ))}
              </div>
            ) : (
              <p className="lot-search-results__empty">Совпадений не найдено. Попробуйте изменить запрос.</p>
            )}
            {lotSearchSuggestions.length > 0 && (
              <div className="lot-search-suggestions">
                <div className="lot-search-suggestions__heading">Близкие формулировки и коды ОКПД2</div>
                {lotSearchSuggestions.map((suggestion) => (
                  <button
                    className="lot-search-suggestion"
                    type="button"
                    key={suggestion.phrase}
                    onClick={() => searchSuggestedPhrase(suggestion.phrase)}
                  >
                    <span className="lot-search-suggestion__main">
                      <strong>{suggestion.phrase}</strong>
                      <small>{suggestion.reason}</small>
                      {suggestion.okpd2_codes.length > 0 && (
                        <span className="lot-search-suggestion__codes">
                          {suggestion.okpd2_codes.map((code) => <span key={code}>{code}</span>)}
                        </span>
                      )}
                    </span>
                    <ChevronRight size={16} />
                  </button>
                ))}
              </div>
            )}
          </section>
        )}

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
                  <a
                    className="secondary-button"
                    href={`${API_BASE}/api/v1/batch/runs/${encodeURIComponent(recommendation.run_id)}/export?format=xlsx`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <ArrowDownToLine size={16} /> Экспорт XLSX
                  </a>
                  <a
                    className="secondary-button"
                    href={`${API_BASE}/api/v1/batch/runs/${encodeURIComponent(recommendation.run_id)}/passport`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <FileText size={16} /> Паспорт доказательств
                  </a>
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
                          onOpenLot={(lotId) => void openEvidenceLot(lotId)}
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
      </footer>
      <Dialog
        open={selectedLotId !== null}
        onOpenChange={(open) => {
          if (!open) setSelectedLotId(null);
        }}
      >
        <DialogContent className="lot-dialog">
          <DialogHeader className="lot-dialog__header">
            <DialogTitle className="lot-dialog__title">
              {selectedLot ? selectedLot.procedure_name || selectedLot.subject || `Лот ${selectedLotId}` : `Лот ${selectedLotId}`}
            </DialogTitle>
          </DialogHeader>
          {selectedLotLoading && <p className="lot-dialog__message">Загружаем данные лота…</p>}
          {selectedLotError && <p className="lot-dialog__message lot-dialog__message--error">{selectedLotError}</p>}
          {selectedLot && (
            <div className="lot-dialog__body">
              <dl className="lot-dialog__facts">
                <div><dt>ID лота</dt><dd>{selectedLot.lot_id}</dd></div>
                <div><dt>Дата публикации</dt><dd>{formatDate(selectedLot.publish_date)}</dd></div>
                <div><dt>Начальная цена</dt><dd>{formatMoney(selectedLot.start_price)}</dd></div>
                <div><dt>ИНН заказчика</dt><dd>{selectedLot.customer_inn || "Не указан"}</dd></div>
                <div><dt>КПП заказчика</dt><dd>{selectedLot.customer_kpp || "Не указан"}</dd></div>
                <div><dt>Закупка МСП</dt><dd>{selectedLot.is_smp === 1 ? "Да" : selectedLot.is_smp === 0 ? "Нет" : "Не указано"}</dd></div>
              </dl>
              <section className="lot-dialog__positions">
                <h3>Позиции ТРУ · {selectedLot.items.length}</h3>
                <ol>
                  {selectedLot.items.map((item) => (
                    <li key={`${item.pos}-${item.okpd2_code}`}>
                      <span>{item.product_name || "Без названия"}</span>
                      <small>{item.okpd2_code || "ОКПД2 не указан"} · вес {item.weight}</small>
                    </li>
                  ))}
                </ol>
              </section>
              <label className="lot-dialog__json-label" htmlFor="lot-json">JSON лота</label>
              <textarea
                id="lot-json"
                className="lot-dialog__json"
                readOnly
                value={JSON.stringify(selectedLot, null, 2)}
                spellCheck={false}
              />
            </div>
          )}
        </DialogContent>
      </Dialog>
      <Dialog open={datasetDialogOpen} onOpenChange={setDatasetDialogOpen}>
        <DialogContent className="dataset-dialog">
          <DialogHeader className="dataset-dialog__header">
            <DialogTitle className="dataset-dialog__title">Замена набора данных</DialogTitle>
            <DialogDescription className="dataset-dialog__description">
              Три CSV будут проверены и загружены вместо текущих данных. Перед заменой автоматически сохраняется снимок SQLite.
            </DialogDescription>
          </DialogHeader>
          <div className="dataset-dialog__body">
            <form className="dataset-upload-form" onSubmit={replaceDataset}>
              <label className="dataset-file-field">
                <span>Извещения</span>
                <input type="file" accept=".csv,text/csv" required onChange={(event) => setNoticesCsv(event.target.files?.[0] ?? null)} />
                {noticesCsv && <small>{noticesCsv.name} · {formatNumber(noticesCsv.size / 1024 / 1024)} МБ</small>}
              </label>
              <label className="dataset-file-field">
                <span>Позиции ТРУ</span>
                <input type="file" accept=".csv,text/csv" required onChange={(event) => setItemsCsv(event.target.files?.[0] ?? null)} />
                {itemsCsv && <small>{itemsCsv.name} · {formatNumber(itemsCsv.size / 1024 / 1024)} МБ</small>}
              </label>
              <label className="dataset-file-field">
                <span>Поставщики и победители</span>
                <input type="file" accept=".csv,text/csv" required onChange={(event) => setSuppliersCsv(event.target.files?.[0] ?? null)} />
                {suppliersCsv && <small>{suppliersCsv.name} · {formatNumber(suppliersCsv.size / 1024 / 1024)} МБ</small>}
              </label>
              <p className="dataset-dialog__limits">До 512 МБ на файл, 1 ГБ суммарно. Некорректные CSV не изменят базу.</p>
              {datasetError && <p className="dataset-dialog__message is-error" role="alert">{datasetError}</p>}
              {datasetMessage && <p className="dataset-dialog__message is-success" role="status">{datasetMessage}</p>}
              <button
                className="primary-button dataset-submit"
                type="submit"
                disabled={datasetAction !== null || !noticesCsv || !itemsCsv || !suppliersCsv}
              >
                {datasetAction === "replace" ? <LoaderCircle className="spin" size={16} /> : <Database size={16} />}
                {datasetAction === "replace" ? "Проверяем и заменяем…" : "Заменить текущую базу"}
              </button>
            </form>
            <section className="dataset-rollback">
              <div>
                <strong>Последний снимок</strong>
                <span>
                  {backupStatus.available
                    ? `Создан ${formatDate(backupStatus.created_at)} · данные ${backupStatus.data_version ?? "без версии"}`
                    : "Снимка пока нет. Он появится после первой успешной замены."}
                </span>
              </div>
              <button
                className="secondary-button"
                type="button"
                disabled={!backupStatus.available || datasetAction !== null}
                onClick={() => void rollbackDataset()}
              >
                {datasetAction === "rollback" ? <LoaderCircle className="spin" size={15} /> : <ArrowDownToLine size={15} />}
                {datasetAction === "rollback" ? "Восстанавливаем…" : "Откатить замену"}
              </button>
            </section>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function FragmentRow({
  item,
  expanded,
  onToggle,
  onOpenLot,
  loading,
  profile,
  profileError,
}: {
  item: Recommendation;
  expanded: boolean;
  onToggle: () => void;
  onOpenLot: (lotId: string) => void;
  loading: boolean;
  profile: SupplierProfile | null;
  profileError: string | null;
}) {
  const [expandedFactor, setExpandedFactor] = useState<string | null>(null);

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
                {item.score_limitations && item.score_limitations.length > 0 && (
                  <section className="score-limitations" aria-label="Причины снижения оценки">
                    <div className="score-limitations__heading">
                      <TrendingDown size={14} /> Что снизило оценку
                    </div>
                    {item.score_limitations.slice(0, 3).map((limitation) => (
                      <button
                        className="score-limitation"
                        type="button"
                        key={limitation.factor}
                        onClick={() => setExpandedFactor(limitation.factor)}
                      >
                        <span className="score-limitation__copy">
                          <strong>{FACTOR_LABELS[limitation.factor] ?? limitation.factor}</strong>
                          <span>{limitation.reason}</span>
                        </span>
                        <span className="score-limitation__impact">
                          −{formatFactorValue(limitation.lost_points)} балла
                        </span>
                      </button>
                    ))}
                  </section>
                )}
                {item.score_modifier && item.score_modifier.lost_points > 0 && (
                  <p className="score-modifier">
                    Общий коэффициент ×{formatFactorValue(item.score_modifier.value)}: {item.score_modifier.reason}
                    {` −${formatFactorValue(item.score_modifier.lost_points)} балла`}
                  </p>
                )}
                <div className="factor-list">
                  {Object.entries(item.factor_scores).map(([factor, value]) => {
                    const details = item.factor_details?.[factor];
                    const isExpanded = expandedFactor === factor;
                    return (
                      <div className="factor-entry" key={factor}>
                        <button
                          className="factor-line"
                          type="button"
                          aria-expanded={isExpanded}
                          onClick={() => setExpandedFactor(isExpanded ? null : factor)}
                        >
                          <span>{FACTOR_LABELS[factor] ?? factor}</span>
                          <span className="factor-line__bar"><span style={{ width: `${Math.round(value * 100)}%` }} /></span>
                          <strong>{Math.round(value * 100)}%</strong>
                          <ChevronDown className={isExpanded ? "factor-line__chevron is-open" : "factor-line__chevron"} size={13} />
                        </button>
                        {isExpanded && (
                          <div className="factor-detail">
                            {details ? (
                              <>
                                <p className="factor-detail__formula">{details.formula}</p>
                                <p className="factor-detail__explanation">{FACTOR_EXPLANATIONS[factor] ?? "Показано фактическое правило расчёта этого фактора."}</p>
                                <div className="factor-detail__summary">
                                  <span className="factor-detail__metric">
                                    <span>Вес критерия <strong>{Math.round(details.weight * 100)}%</strong></span>
                                    <small>Доля фактора в сумме весов рейтинга.</small>
                                  </span>
                                  <span className="factor-detail__metric">
                                    <span>Вклад с учётом ограничений <strong>{details.final_contribution.toFixed(2)} балла</strong></span>
                                    <small>Баллы этого фактора после коэффициента истории и проверки РНП.</small>
                                  </span>
                                </div>
                                <dl className="factor-detail__inputs">
                                  {details.inputs.map((input) => (
                                    <div key={input.label}>
                                      <dt>{input.label}</dt>
                                      <dd>{formatFactorValue(input.value)}</dd>
                                    </div>
                                  ))}
                                </dl>
                                {details.evidence.length > 0 && (
                                  <div className="factor-detail__evidence">
                                    <strong>Подтверждающие данные</strong>
                                    {details.evidence.map((evidence, index) => (
                                      <div className="factor-detail__evidence-row" key={`${evidence.lot_id ?? factor}-${index}`}>
                                        {evidence.lot_id ? (
                                          <button type="button" onClick={() => onOpenLot(evidence.lot_id!)}>
                                            Лот {evidence.lot_id}
                                          </button>
                                        ) : <span>Историческая запись</span>}
                                        <span>{evidence.publish_date ? formatDate(evidence.publish_date) : ""}</span>
                                        {evidence.title && <span className="factor-detail__evidence-title">{evidence.title}</span>}
                                        <small>
                                          {evidence.item_coverage != null && `ОКПД2 ${Math.round(evidence.item_coverage * 100)}% · `}
                                          {evidence.text_similarity != null && `текст ${Math.round(evidence.text_similarity * 100)}% · `}
                                          {evidence.recency_weight != null && `давность ×${evidence.recency_weight} · `}
                                          {evidence.retrieval_weight != null && `вес выборки ${evidence.retrieval_weight} · `}
                                          {evidence.contribution != null && `вклад ${evidence.contribution} · `}
                                          {evidence.price != null && formatMoney(evidence.price)}
                                        </small>
                                      </div>
                                    ))}
                                  </div>
                                )}
                              </>
                            ) : (
                              <p className="factor-detail__formula">Детали расчёта для этого запуска недоступны.</p>
                            )}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </section>
              <section className="detail-pane detail-pane--wide">
                <div className="detail-pane__title"><Activity size={16} /> Доказательства и реестры</div>
                {item.evidence.length ? (
                  <div className="evidence-list">
                    {item.evidence.map((evidence) => (
                      <article className="evidence-item" key={evidence.lot_id}>
                        <div className="evidence-item__top">
                          <button className="evidence-lot-link" type="button" onClick={() => onOpenLot(evidence.lot_id)}>
                            Лот {evidence.lot_id} <ArrowUpRight size={13} />
                          </button>
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