import { writeFile } from 'node:fs/promises';

const etfs = [
  { code: '441640', name: 'KODEX 미국배당커버드콜액티브', yieldRate: 9.63 },
  { code: '498400', name: 'KODEX 200타겟위클리커버드콜', yieldRate: 15.06 }
];

async function fetchQuote(etf) {
  const response = await fetch(`https://m.stock.naver.com/api/stock/${etf.code}/basic`, {
    headers: {
      accept: 'application/json, text/plain, */*',
      'user-agent': 'Mozilla/5.0 (compatible; millions-dividend-tracker/1.0)'
    }
  });
  if (!response.ok) throw new Error(`${etf.code}: HTTP ${response.status}`);
  const body = await response.json();
  const price = Number(String(body?.closePrice || '').replace(/[^0-9.-]/g, ''));
  if (!(price > 0)) throw new Error(`${etf.code}: price unavailable`);
  return { ...etf, price, asOf: body.localTradedAt || '' };
}

const quotes = await Promise.all(etfs.map(fetchQuote));
const output = {
  updatedAt: new Date().toISOString(),
  source: 'Naver Finance',
  etfs: Object.fromEntries(quotes.map(quote => [quote.code, {
    name: quote.name,
    price: quote.price,
    asOf: quote.asOf,
    yieldRate: quote.yieldRate
  }]))
};

await writeFile('market-data.json', `${JSON.stringify(output, null, 2)}\n`);
