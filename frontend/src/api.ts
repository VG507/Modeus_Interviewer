export type Topic = { id: string; profession_id: string; title: string };
export type Context = { role: string; experience: string; responsibilities: string; conditions: string };
export type Message = {
  id: string; kind: 'question' | 'answer' | 'control'; text: string | null;
  reply_to_question_id: string | null;
  status: 'draft' | 'queued' | 'processing' | 'processed' | 'failed';
  assessment?: 'relevant' | 'unclear' | 'unrelated' | 'declined' | null;
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
export type Card = { id: string; text: string; publication_status: string };
export type Page<T> = { items: T[]; next_cursor: string | null };

const messages: Record<string, string> = {
  VERSION_CONFLICT: 'Интервью изменилось. Проверьте актуальное состояние перед повтором.',
  PROCESSING_IN_PROGRESS: 'Ответ уже обрабатывается.',
  INVALID_STATE: 'Действие недоступно в текущем состоянии интервью.',
  INVALID_CREDENTIALS: 'Проверьте почту и пароль.',
  LOGIN_TEMPORARILY_LOCKED: 'Слишком много попыток. Повторите через 15 минут.',
  UNAUTHENTICATED: 'Сессия закончилась. Войдите снова.',
  INTERVIEW_NOT_FOUND: 'Интервью не найдено или недоступно.',
  TOPIC_NOT_FOUND: 'Тема больше недоступна.',
  ORIGIN_REJECTED: 'Откройте Modeus по адресу, заданному в APP_ORIGIN.',
  VALIDATION_ERROR: 'Проверьте заполнение и длину полей.',
  PROCESSING_TIMEOUT: 'Модель не успела ответить.',
  LLM_UNAVAILABLE: 'Модель недоступна. Если вы используете Ollama, проверьте, что она запущена.',
  INVALID_MODEL_OUTPUT: 'Модель вернула ответ в неподходящем формате.',
};

export class ApiError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(messages[code] ?? `Сервер вернул ошибку HTTP ${status}.`);
  }
}

export function describe(error: unknown) {
  return error instanceof ApiError ? error.message : 'Нет связи с сервером. Проверьте подключение и повторите.';
}

export function processingError(code: string) {
  return messages[code] ?? 'Обработка остановилась.';
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch('/api/v1' + path, { ...options, credentials: 'same-origin' });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new ApiError(response.status, data?.error?.code ?? '');
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
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
