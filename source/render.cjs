// Рендер out/<id>/audit.html в audit.png. Запуск: node karty-agents/render.cjs [id ...]
const path = require('path'), fs = require('fs');
const { chromium } = require(path.join(process.env.HOME, 'Projects/playwright-tools/node_modules/playwright'));
(async () => {
  const out = path.join(__dirname, 'out');
  const ids = process.argv.slice(2).length ? process.argv.slice(2) : fs.readdirSync(out);
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 880, height: 600 }, deviceScaleFactor: 2 });
  for (const id of ids) {
    await page.goto('file://' + path.join(out, id, 'audit.html'));
    await page.locator('.page').screenshot({ path: path.join(out, id, 'audit.png') });
  }
  await browser.close();
  console.log(`PNG: ${ids.length}`);
})();
