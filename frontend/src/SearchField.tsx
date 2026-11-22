import { Input, InputClear, InputGroup } from '@ui-sdk/react/input';

type SearchFieldProps = {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  required?: boolean;
  maxLength?: number;
};

export function SearchField({ id, label, value, onChange, required, maxLength }: SearchFieldProps) {
  return <div>
    <label htmlFor={id}>{label}</label>
    <InputGroup className="search-field">
      <Input id={id} type="search" value={value} onValueChange={onChange}
        required={required} maxLength={maxLength}/>
      <InputClear className="secondary" aria-label={`Очистить поле «${label}»`}>Очистить</InputClear>
    </InputGroup>
  </div>;
}
