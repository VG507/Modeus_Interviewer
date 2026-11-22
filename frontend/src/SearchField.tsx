import { useRef } from 'react';

type SearchFieldProps = {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  maxLength?: number;
};

export function SearchField({ id, label, value, onChange, required, maxLength }: SearchFieldProps) {
  const input = useRef<HTMLInputElement>(null);

  return <div>
    <label htmlFor={id}>{label}</label>
    <div className="search-field">
      <input ref={input} id={id} type="search" value={value} onChange={event => onChange(event.target.value)}
        required={required} maxLength={maxLength}/>
      {value && <button type="button" className="secondary" aria-label={`Очистить поле «${label}»`} onClick={() => {
        onChange('');
        input.current?.focus();
      }}>Очистить</button>}
    </div>
  </div>;
}
