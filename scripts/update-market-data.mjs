import { writeFile } from 'node:fs/promises';

const symbols = ['SCHD', 'DGRO'];

async function fetchQuote(symbol) {
  const response = await fetch(`https://api.nasdaq.com/api/quote/${symbol}/info?assetclass=etf`, {
    headers: {
      accept: 'application/json, text/plain, */*',
      'user-agent': 'Mozilla/5.0 (compatible; millions-dividend-tracker/1.0)'
    }
  });
  if (!response.ok) throw new Error(`${symbol}: HTTP ${response.status}`);
  const body = await response.json();
  const quote = body?.data?.primaryData;
  const price = Number(String(quote?.lastSalePrice || '').replace(/[^0-9.-]/g, ''));
  if (!(price > 0)) throw new Error(`${symbol}: price unavailable`);
  return { price, asOf: quote.lastTradeTimestamp || '' };
}

const [schd, dgro] = await Promise.all(symbols.map(fetchQuote));
const output = {
  schd: schd.price,
  dgro: dgro.price,
  schdAsOf: schd.asOf,
  dgroAsOf: dgro.asOf,
  updatedAt: new Date().toISOString(),
  source: 'Nasdaq'
};

await writeFile('market-data.json', `${JSON.stringify(output, null, 2)}\n`);
