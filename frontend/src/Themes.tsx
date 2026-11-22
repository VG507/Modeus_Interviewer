import { useEffect, useState, type CSSProperties } from 'react';
import { api, describe, skillCategories, type ThemesPage } from './api';

const number = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });

function cases(count: number) {
  const last = count % 10, tens = count % 100;
  if (last === 1 && tens !== 11) return `${count} случай`;
  if (last >= 2 && last <= 4 && (tens < 12 || tens > 14)) return `${count} случая`;
  return `${count} случаев`;
}

// Diverging fill: blue for "more often in this theme", red for "less often", gray for no link.
function cellStyle(phi: number): CSSProperties {
  const pole = phi >= 0 ? 'var(--corr-more)' : 'var(--corr-less)';
  return { background: `color-mix(in oklab, ${pole} ${Math.round(Math.abs(phi) * 100)}%, var(--corr-none))` };
}

export function Themes() {
  const [page, setPage] = useState<ThemesPage>();
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    api<ThemesPage>('/catalog/themes', { signal: controller.signal }).then(setPage).catch(problem => {
      if (!controller.signal.aborted) setError(describe(problem));
    });
    return () => controller.abort();
  }, []);

  return <>
    <a className="back" href="#/catalog">← Каталог</a>
    <h1>Темы и навыки</h1>
    {error ? <p role="alert">{error}</p> : !page ? <p role="status">Собираем темы…</p> : !page.themes.length ?
      <section className="panel"><p>Пока нет похожих случаев. Темы появятся, когда в каталоге будет несколько
        карточек об одной и той же проблеме.</p></section> : <>
      <section className="panel saved" aria-label="Темы">
        <p className="meta">Случаи сгруппированы по смыслу: каждый похож на первый случай темы не меньше
          чем на {page.threshold.toLocaleString('ru-RU')}. Без темы {page.unthemed} из {cases(page.cases)}.</p>
        <ul>{page.themes.map(theme => <li key={theme.card_id + theme.title}>
          <a href={`#/catalog/${theme.card_id}`}>
            {theme.title}
            <span>{[cases(theme.cases), theme.skills.map(item => item.skill).join(', ')].filter(Boolean).join(' · ')}</span>
          </a>
          {theme.cards.length > 1 && <p className="meta">{theme.cards.map((card, index) => <span key={card.id}>
            {index > 0 && ', '}<a className="card-link" href={`#/catalog/${card.id}`}>{card.title}</a>
          </span>)}</p>}
        </li>)}</ul>
      </section>
      {page.categories.length > 0 && <section className="panel" aria-labelledby="theme-matrix">
        <h2 id="theme-matrix">Темы и категории навыков</h2>
        <p className="meta">Корреляция φ от −1 до +1: насколько чаще случаи темы упоминают навыки категории,
          чем остальные случаи каталога. В скобках — сколько случаев темы упоминают категорию.</p>
        <div className="heat-scroll">
          <table className="heat">
            <thead><tr><th scope="col">Тема</th>
              {page.categories.map(category => <th scope="col" key={category}>{skillCategories[category] ?? category}</th>)}
            </tr></thead>
            <tbody>{page.themes.map(theme => <tr key={theme.card_id + theme.title}>
              <th scope="row">{theme.title}</th>
              {theme.matrix.map((cell, index) => {
                const category = skillCategories[page.categories[index]] ?? page.categories[index];
                const tip = `${theme.title} × ${category}: φ ${number.format(cell.phi)}, ${cases(cell.cases)} из ${theme.cases}`;
                return <td key={index} tabIndex={0} aria-label={tip} title={tip} style={cellStyle(cell.phi)}
                  className={Math.abs(cell.phi) >= 0.55 ? 'strong' : undefined}>
                  {number.format(cell.phi)}<small> ({cell.cases})</small>
                </td>;
              })}
            </tr>)}</tbody>
          </table>
        </div>
        <div className="heat-legend" aria-hidden="true">
          <span>−1 реже</span><span className="heat-ramp"/><span>+1 чаще</span>
        </div>
      </section>}
    </>}
  </>;
}
