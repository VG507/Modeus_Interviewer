import { useEffect, useRef, useState } from 'react';

// The browser's Web Speech API. TypeScript's DOM types do not describe it, so only what we use is typed.
type Alternative = { transcript: string };
type Result = { isFinal: boolean; 0: Alternative };
type Recognition = {
  lang: string; continuous: boolean; interimResults: boolean;
  start(): void; stop(): void; abort(): void;
  onresult: ((event: { resultIndex: number; results: ArrayLike<Result> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
};
const speech = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
const SpeechRecognition = speech.SpeechRecognition ?? speech.webkitSpeechRecognition;

const errors: Record<string, string> = {
  'not-allowed': 'Браузер не дал доступ к микрофону. Разрешите его в настройках сайта.',
  'service-not-allowed': 'Браузер не разрешает распознавание речи на этой странице.',
  'audio-capture': 'Микрофон не найден.',
  'no-speech': 'Речь не услышана. Попробуйте ещё раз.',
  network: 'Нет связи с сервисом распознавания браузера.',
  'language-not-supported': 'Браузер не распознаёт русскую речь.',
};

// Speech typed into the answer: finished phrases go to onText, the phrase still being spoken shows below.
// Firefox has no Web Speech API, so there is no button there.
export function Dictation({ disabled, onText }: { disabled: boolean; onText: (text: string) => void }) {
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState('');
  const recognition = useRef<Recognition | null>(null);
  const deliver = useRef(onText);
  deliver.current = onText;

  useEffect(() => () => recognition.current?.abort(), []);
  useEffect(() => { if (disabled) recognition.current?.stop(); }, [disabled]);

  if (!SpeechRecognition) return null;

  function start() {
    const current = new SpeechRecognition!();
    current.lang = 'ru-RU';
    current.continuous = true;
    current.interimResults = true;
    current.onresult = event => {
      let spoken = '';
      for (let index = event.resultIndex; index < event.results.length; index++) {
        const result = event.results[index];
        if (result.isFinal) deliver.current(result[0].transcript.trim());
        else spoken += result[0].transcript;
      }
      setInterim(spoken);
    };
    current.onerror = event => { if (event.error !== 'aborted') setError(errors[event.error] ?? 'Не удалось распознать речь.'); };
    current.onend = () => {
      recognition.current = null;
      setListening(false);
      setInterim('');
    };
    recognition.current = current;
    setError('');
    current.start();
    setListening(true);
  }

  return <div className="dictation">
    <button type="button" className="secondary" aria-pressed={listening} disabled={disabled && !listening}
      onClick={() => listening ? recognition.current?.stop() : start()}>
      {listening ? 'Остановить диктовку' : 'Надиктовать'}
    </button>
    {listening && <p className="meta" role="status">
      {interim || 'Слушаю…'} <span>Речь распознаёт браузер: Chrome отправляет звук в Google, Safari — в Apple.</span>
    </p>}
    {error && <p role="alert">{error}</p>}
  </div>;
}
