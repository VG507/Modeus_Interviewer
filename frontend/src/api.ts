export type Topic = { id: string; profession_id: string; title: string };
export type ProjectContext = {
  task: string; period: string; role: string; responsibility: string; stack: string; constraints: string;
};
export const tones = {
  friendly: 'Дружелюбный', business: 'Деловой', playful: 'Шутливый',
} as const;
export type Tone = keyof typeof tones;
export type Context = {
  role: string; experience: string; responsibilities: string; conditions: string; projects?: ProjectContext[];
  tone?: Tone;
};
export type Message = {
  id: string; kind: 'question' | 'answer' | 'control'; text: string | null;
  reply_to_question_id: string | null;
  status: 'draft' | 'queued' | 'processing' | 'processed' | 'failed';
  assessment?: 'relevant' | 'unclear' | 'unrelated' | 'declined' | null;
  vacancy_quote?: { title: string; url: string; quote: string } | null;
  source_quote?: { title: string; url: string; quote: string } | null;
};
export type Interview = {
  id: string; topic_id: string; context: Context; version: number; updated_at: string;
  current_topic_title: string | null;
  status: 'active' | 'completing' | 'completed';
  processing_status: 'idle' | 'queued' | 'running' | 'failed';
  processing_stage: 'initial_question' | 'extraction' | 'question' | 'synthesis' | null;
  current_question_id: string | null; card_id: string | null;
  error: { code: string; message: string; retryable: boolean } | null;
};
export type Detail = Interview & { messages: Message[] };
export type AnswerResult = { interview: Interview; answer: Message };
export type CardGap = 'task' | 'context' | 'action' | 'decision' | 'verification' | 'error' | 'limitation';
export type CardFact = { id?: string; text: string; skills?: string[]; derived_from_fact_id?: string };
export type CardEvidence = {
  original_text: string | null; skills: string[];
  turns: { question: string; answer_quote: string; source: Message['source_quote'] }[];
};
export type CardContent = { title: string; case_id?: string; cases?: CardContent[]; unknown_fields?: CardGap[] } & Partial<Record<
  'task' | 'context' | 'actions' | 'decisions' | 'verification' | 'errors' | 'limitations',
  CardFact[]
>>;
export type Card = {
  id: string; session_id: string; owner_id: string; version: number; content_version: number;
  text: string; content: CardContent; show_author: boolean; author_name: string | null;
  publication_status: 'draft' | 'in_review' | 'published';
  review_status: 'unreviewed' | 'needs_changes' | 'reviewed';
  review_blocker: string | null;
  consent: { share_evidence?: boolean } | null;
  review: { decision: 'accept' | 'return' | 'keep_private' | 'unpublish'; comment: string } | null;
};
export type CardDetail = Card & {
  original: { content: CardContent };
  history: { details: { previous?: { content: CardContent } } }[];
};
export type PublicSummary = {
  id: string; title: string; profession_id: string; topic_title: string;
  author_name: string | null; reviewed_at: string;
};
export type PublicCard = PublicSummary & { content: CardContent; text: string };
export type Theme = {
  title: string; card_id: string; cases: number; cards: PublicSummary[];
  skills: { skill: string; cases: number }[]; matrix: { phi: number; cases: number }[];
};
export type ThemesPage = { threshold: number; cases: number; unthemed: number; categories: string[]; themes: Theme[] };
export type SearchMatch = { title: string; task: string; skills: string[] };
export type SearchPage = {
  items: (PublicSummary & { score: number; match?: SearchMatch | null })[]; mode: 'meaning' | 'words';
};
export type Page<T> = { items: T[]; next_cursor: string | null };

const messages: Record<string, string> = {
  VERSION_CONFLICT: 'Данные изменились. Проверьте актуальное состояние перед повтором.',
  PROCESSING_IN_PROGRESS: 'Ответ уже обрабатывается.',
  INVALID_STATE: 'Действие недоступно в текущем состоянии.',
  INVALID_CREDENTIALS: 'Проверьте почту и пароль.',
  LOGIN_TEMPORARILY_LOCKED: 'Слишком много попыток. Повторите через 15 минут.',
  LOGIN_RATE_LIMIT: 'Слишком много попыток входа. Повторите позже.',
  ACCOUNT_TEMPORARILY_LOCKED: 'Аккаунт временно заблокирован.',
  ACTIVE_INTERVIEW_LIMIT: 'Завершите одно из активных интервью, чтобы начать новое.',
  INTERVIEW_PROFILE_REQUIRED: 'Сначала заполните анкету о вашей работе.',
  PAYLOAD_TOO_LARGE: 'Запрос слишком большой. Сократите данные и повторите.',
  CURSOR_INVALID: 'Список изменился. Откройте его заново.',
  INTERNAL_SERVER_ERROR: 'Произошла ошибка на сервере.',
  API_UNREACHABLE: 'Нет связи с backend Modeus на порту 8000. Проверьте, запущен ли он.',
  UNAUTHENTICATED: 'Сессия закончилась. Войдите снова.',
  INTERVIEW_NOT_FOUND: 'Интервью не найдено или недоступно.',
  TOPIC_NOT_FOUND: 'Тема больше недоступна.',
  CARD_NOT_FOUND: 'Карточка не найдена или недоступна.',
  CARD_UNCHANGED: 'В карточке нет изменений.',
  COLLECTION_RETRY_UNAVAILABLE: 'Этот этап уже изменился. Обновите состояние сбора.',
  EDITOR_REQUIRED: 'Для этого действия нужен доступ редактора.',
  ORIGIN_REJECTED: 'Откройте Modeus по адресу, заданному в APP_ORIGIN.',
  VALIDATION_ERROR: 'Проверьте заполнение и длину полей.',
  PROCESSING_TIMEOUT: 'Модель не успела ответить.',
  LLM_UNAVAILABLE: 'Модель недоступна. Если вы используете Ollama, проверьте, что она запущена.',
  INVALID_MODEL_OUTPUT: 'Модель вернула ответ в неподходящем формате.',
  DISCOVERY_BUSY: 'Сбор из этого источника уже выполняется. Попробуйте позже.',
  DISCOVERY_RATE_LIMIT: 'Источник ограничил запросы. Попробуйте позже.',
  DISCOVERY_TIMEOUT: 'Источник не ответил вовремя. Попробуйте позже.',
  DISCOVERY_QUERY_INVALID: 'Проверьте формат репозиториев или тем GitHub.',
  DISCOVERY_UNAVAILABLE: 'Источник временно недоступен или вернул неожиданный ответ.',
  DOCUMENTATION_TARGET_INVALID: 'Выберите источники из списка официальной документации.',
  DOCUMENTATION_QUOTE_INVALID: 'Цитата должна дословно встречаться в тексте раздела, а деталь — в цитате.',
  DOCUMENTATION_REVISION_CHANGED: 'Редакция источника изменилась. Откройте актуальную версию для проверки.',
  PROFILE_NOT_FOUND: 'Профессия не найдена.',
  REPORT_NO_DATA: 'За выбранный период по этой профессии нет вакансий, карточек и источников.',
  REPORT_DEMO_NOT_FOUND: 'Для этой профессии нет демо-данных.',
};

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string, retryAfter?: number, message?: string) {
    super(code === 'DISCOVERY_RATE_LIMIT' && retryAfter !== undefined ?
      `Источник ограничил запросы. Повторите не раньше чем через ${retryAfter} сек.` :
      messages[code] ?? message ?? `Сервер вернул ошибку HTTP ${status}.`);
  }
}

export function describe(error: unknown) {
  return error instanceof ApiError ? error.message : 'Нет связи с сервером. Проверьте подключение и повторите.';
}

export function processingError(code: string) {
  return messages[code] ?? 'Обработка остановилась.';
}

async function failure(response: Response) {
  const data = await response.json().catch(() => null);
  const retryAfter = response.headers.get('Retry-After');
  const code = data?.error?.code ?? '';
  return new ApiError(response.status, code, retryAfter && /^\d+$/.test(retryAfter) ?
    Number(retryAfter) : undefined, code === 'DOCUMENTATION_REVIEW_INVALID' ? data.error.message : undefined);
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api/v1' + path, { ...options, credentials: 'same-origin' });
  if (!response.ok) throw await failure(response);
  return (response.status === 204 ? undefined : await response.json()) as T;
}

// A file the server builds for a request, saved under the name the server gives it.
export async function download(path: string, body: object, fallbackName: string) {
  const response = await fetch('/api/v1' + path, {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!response.ok) throw await failure(response);
  const name = response.headers.get('Content-Disposition')?.match(/filename="([^"]+)"/)?.[1] ?? fallbackName;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(await response.blob());
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 60_000);
  return name;
}

export function command<T>(path: string, body: object, key?: string, method = 'POST') {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (key) headers['Idempotency-Key'] = key;
  return api<T>(path, { method, headers, body: JSON.stringify(body) });
}

// Keep the same key if the server might have accepted a request before the connection dropped.
export function commandKeys() {
  const pending = new Map<string, string>();
  return async function execute<T>(path: string, body: object, method = 'POST') {
    const fingerprint = JSON.stringify([method, path, body]);
    const key = pending.get(fingerprint) ?? crypto.randomUUID();
    pending.set(fingerprint, key);
    try {
      const result = await command<T>(path, body, key, method);
      pending.delete(fingerprint);
      return result;
    } catch (error) {
      if (error instanceof ApiError && error.status < 500) pending.delete(fingerprint);
      throw error;
    }
  };
}

export const skillCategories: Record<string, string> = {
  languages: 'Языки', backend: 'Бэкенд', frontend: 'Фронтенд', databases: 'Базы данных',
  messaging: 'Очереди и брокеры', devops: 'DevOps', testing: 'Тестирование', tools: 'Инструменты', other: 'Другое',
};
