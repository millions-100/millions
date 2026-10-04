import { writeFile } from 'node:fs/promises';

async function fetchQuote() {
  const response = await fetch('https://m.stock.naver.com/api/stock/458730/basic', {
    headers: {
      accept: 'application/json, text/plain, */*',
      'user-agent': 'Mozilla/5.0 (compatible; millions-dividend-tracker/1.0)'
    }
  });
  if (!response.ok) throw new Error(`458730: HTTP ${response.status}`);
  const body = await response.json();
  const price = Number(String(body?.closePrice || '').replace(/[^0-9.-]/g, ''));
  if (!(price > 0)) throw new Error('458730: price unavailable');
  return { price, asOf: body.localTradedAt || '' };
}

const quote = await fetchQuote();
const output = {
  code: '458730',
  name: 'TIGER 미국배당다우존스',
  price: quote.price,
  asOf: quote.asOf,
  updatedAt: new Date().toISOString(),
  source: 'Naver Finance'
};

await writeFile('market-data.json', `${JSON.stringify(output, null, 2)}\n`);
